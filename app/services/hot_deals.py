"""Hot deals ("Revente - Offre Chaude"): a courier resells the goods of an order whose customer
stopped answering; another customer reserves them. Ports of base44/functions/createHotDeal,
reserveHotDeal, listHotDeals and the ResaleOrder part of sweepExpiredTestData.

createHotDeal (courier of the order): only goods already bought (purchased / on_the_way /
  client_no_response) of a reported order, only once the no-response deadline is past and the
  customer silent (the case is brought up to date first, like the live `status` call); prices,
  goods and position come from the database, the client picks the discount (0-90 %, default 0)
  and the delivery fee (0.5-100, default 3); the photo must be one of the courier's own public
  uploads. The deal lives 2 h. The original order is cancelled (courier, 'client_no_response',
  note "Remis en vente"), the case resolved 'resold' with the incident counted, the customer
  told (always pushed). All under the order lock: a customer confirming at the same second is
  either before (→ 409 customer_answered) or after (→ 409 too_late).
reserveHotDeal (any other signed-in user): deal row locked, so two buyers of the same deal get
  one order and one 409; at most MAX_ACTIVE_RESERVATIONS hot-deal orders still running per
  buyer (buyer row locked: two reservations at once can't both pass). The buyer's order is
  created 'accepted' with the deal's courier (ot.start), its total = discounted price + the
  deal's delivery fee; the courier is told (hot_deal_reserved, always pushed).
listHotDeals: listed deals not expired, public fields only, nearest first (PostGIS), 0.1 km.
Jobs: expire_deals (hourly: listed deals past their expiry → expired) and purge_deals (purge
  step "hot_deals" of test_data_purge: expired unsold deals and QA deals are deleted).
"""

import math
import re
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Boolean, Numeric, and_, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.compat.jsnum import clamp, is_finite_number, js_number, number_or
from app.models import Courier, File, HotDeal, NoResponseCase, Order, OrderStop, User
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import cancellation, no_response
from app.services import order_transitions as ot
from app.services.geo import point
from app.services.order_notices import notify_always_pushed
from app.services.orders import TEST_ORDER_SQL, courier_of_user, first_stop, mirror_incidents
from app.services.phones import InvalidPhone, to_e164
from app.services.shops import key_from_public_url, public_prefix

RESELLABLE = frozenset({"purchased", "on_the_way", "client_no_response"})
TTL = timedelta(hours=2)
MAX_DISCOUNT = 90
FEE_BOUNDS = (0.5, 100.0)
DEFAULT_FEE = 3.0
MAX_ACTIVE_RESERVATIONS = 2
# Hot-deal orders still running for their buyer (reserveHotDeal ACTIVE_ORDER_STATUSES, plus
# client_no_response: a parked delivery is still running).
ACTIVE_ORDER_STATUSES = (
    "pending", "offers_received", "accepted", "at_shop", "price_confirmation_needed", "purchased",
    "on_the_way", "client_no_response",
)  # fmt: skip
# Coordinates of the buyer's address: inside Tunisia with a margin, else unknown.
BUYER_BOUNDS = (30.0, 37.6, 7.5, 12.0)
PHONE_SHAPE = r"^\+?[0-9 ]{8,20}$"
LIST_RADIUS_DEFAULT = 50
LIST_RADIUS_MAX = 200
LIST_LIMIT_DEFAULT = 30
LIST_LIMIT_MAX = 50
PUBLIC_FIELDS = (
    "id", "items_text", "shop_name", "purchase_amount", "discount_percentage", "discounted_price",
    "include_delivery", "delivery_fee", "photo_url", "expires_at", "status", "courier_name",
    "created_date",
)  # fmt: skip
PURGE_BATCH = 500

Result = tuple[int, dict[str, Any]]


def announce(session: AsyncSession, deal: HotDeal | uuid.UUID, status: str, created: bool = False) -> None:
    """Realtime ResaleOrder event. Only listed deals are readable by everyone: a deal leaving the
    listing is announced as `delete` (for every non-admin subscriber the row is gone), so the
    customers' lists (HotDealsSection) reload."""
    deal_id = deal.id if isinstance(deal, HotDeal) else deal
    if status == "available":
        emit(session, "ResaleOrder", "create" if created else "update", deal_id)
    else:
        emit(session, "ResaleOrder", "delete", deal_id)


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _bounded(payload: dict[str, Any], key: str, low: float, high: float, fallback: float) -> float:
    """createHotDeal's clamp(): `Number(x)` finite → bounded, else the fallback (absent = NaN)."""
    if key not in payload:
        return fallback
    number = js_number(payload[key])
    return clamp(number, low, high) if math.isfinite(number) else fallback


def _js_num_text(value: Decimal | None) -> str:
    number = float(value or 0)
    return str(int(number)) if number.is_integer() else str(number)


async def _ewkt(session: AsyncSession, column: Any, where: Any) -> str | None:
    """A stored point as EWKT text, to copy it onto another row (no shapely needed)."""
    return (await session.execute(select(func.ST_AsEWKT(column)).where(where))).scalar_one_or_none()


async def _photo_key(session: AsyncSession, url: Any, user: CurrentUser) -> str | None:
    """Shown to every customer as an image: only one of the courier's own public uploads."""
    key = key_from_public_url(url)
    if key is None:
        return None
    owner = (await session.execute(select(File.owner_id).where(File.key == key))).first()
    return key if owner is not None and owner[0] == user.id else None


# ─────────────────────────── createHotDeal ───────────────────────────


async def create_deal(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return 400, {"error": "Missing order_id"}
    order = await ot.lock_order(session, order_id)
    if order is None:
        return 404, {"error": "Order not found"}
    courier = await courier_of_user(session, user.id)
    if courier is None or order.courier_id != courier.id:
        return 403, {"error": "Only the courier of this order can resell it"}

    reported = (
        await session.execute(select(NoResponseCase.id).where(NoResponseCase.order_id == order.id).limit(1))
    ).first() is not None
    purchase = order.purchase_amount or Decimal(0)
    no_report = not (order.status == "client_no_response" or reported)
    if order.status not in RESELLABLE or no_report or purchase <= 0:
        return 409, {"error": "order_not_resellable", "status": order.status}

    # The waiting time is enforced here, not by the courier's screen: the case is brought up to
    # date (incident + notifications when the deadline just passed, as the live `status` call).
    await no_response.refresh(session, order)
    case = await no_response.latest_case(session, order.id)
    gate = cancellation.no_response_gate(order.status, case, ot.now_utc())
    if not gate["ok"] or order.status not in RESELLABLE:
        no_response.keep_writes(session)  # what the refresh recorded stays
        return 409, {
            "error": gate["reason"] if not gate["ok"] else "order_not_resellable",
            "deadline_at": gate.get("deadline_at"),
            "status": order.status,
        }

    live = (
        await session.execute(
            select(HotDeal.id).where(HotDeal.original_order_id == order.id, HotDeal.status == "available")
        )
    ).first()
    if live is not None:
        return 409, {"error": "already_listed", "deal_id": str(live[0])}

    discount = _bounded(payload, "discount_percentage", 0, MAX_DISCOUNT, 0)
    fee = _bounded(payload, "delivery_fee", *FEE_BOUNDS, DEFAULT_FEE)
    now = ot.now_utc()
    stop = await first_stop(session, order.id)
    deal = HotDeal(
        original_order_id=order.id,
        courier_id=courier.id,
        items_text=order.items_text,
        shop_name=stop.name if stop else None,
        shop_address=stop.address if stop else None,
        purchase_amount=purchase,
        discount_percentage=_money(discount),
        price=_money(float(purchase) * (1 - discount / 100)),
        include_delivery=True,
        delivery_fee=_money(fee),
        photo_key=await _photo_key(session, payload.get("photo_url"), user),
        pickup_location=await _ewkt(session, Courier.last_location, Courier.id == courier.id),
        status="available",
        expires_at=now + TTL,
    )
    session.add(deal)
    await session.flush()
    announce(session, deal, "available", created=True)

    note = "تم إعادة عرضه للبيع" if payload.get("lang") == "ar" else "Remis en vente"
    order.notes = f"{order.notes} | {note}" if order.notes else note
    order.cancelled_by, order.cancel_reason = "courier", "client_no_response"
    await ot.transition(
        session, order, "cancelled", user, "createHotDeal", "client_no_response", cancelled_by="courier"
    )
    if case is not None and case.status != "resolved":
        # the courier's choice closes the case; the incident stays counted
        case.status, case.resolution, case.resolved_at = "resolved", "resold", now
        case.incident_counted = case.messaging_status != no_response.LEGACY
        case.final_at = case.final_at or now
        await session.flush()
        emit(session, "NoResponseCase", "update", case.id)
        await mirror_incidents(session, order.customer_id)

    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        order_id=order.id,
        type_="order_cancelled",
        title_ar="❌ تم إلغاء طلبك",
        title_fr="❌ Commande annulée",
        body_ar=(
            "لم تردّ على المندوب رغم الإشعار ورسائل واتساب/SMS، فتم إلغاء الطلب وإعادة عرض المشتريات "
            "للبيع. سُجّلت حادثة عدم رد."
        ),
        body_fr=(
            "Vous n'avez pas répondu au livreur malgré la notification et WhatsApp/SMS : la commande "
            "est annulée, les achats sont remis en vente et un incident de non-réponse est enregistré."
        ),
        metadata={
            "reason": "client_no_response",
            "resale_order_id": str(deal.id),
            "recipient_role": "customer",
        },
    )
    return 200, {"success": True, "deal_id": str(deal.id)}


# ─────────────────────────── reserveHotDeal ───────────────────────────


def _coord(value: Any, low: float, high: float) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    number = js_number(value)
    return number if math.isfinite(number) and low <= number <= high else None


def _buyer_phone(raw: Any, fallback: str | None) -> str | None:
    if isinstance(raw, str) and re.fullmatch(PHONE_SHAPE, raw.strip()):
        try:
            return to_e164(raw.strip()) or fallback
        except InvalidPhone:
            return fallback
    return fallback


async def _active_reservations(session: AsyncSession, buyer_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.customer_id == buyer_id,
                Order.resale_deal_id.is_not(None),
                Order.status.in_(ACTIVE_ORDER_STATUSES),
            )
        )
    ).scalar_one()


async def reserve_deal(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    raw_id = payload.get("resale_order_id")
    address = payload.get("delivery_address")
    address = address.strip()[:300] if isinstance(address, str) else ""
    if not raw_id or not address:
        return 400, {"error": "Missing required fields"}
    try:
        deal_id = uuid.UUID(str(raw_id))
    except ValueError:
        return 404, {"error": "Hot deal not found"}
    deal = (
        await session.execute(
            select(HotDeal)
            .where(HotDeal.id == deal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if deal is None:
        return 404, {"error": "Hot deal not found"}
    if deal.status != "available":
        return 409, {"error": "Hot deal is no longer available"}
    now = ot.now_utc()
    if deal.expires_at <= now:
        deal.status = "expired"
        await session.flush()
        announce(session, deal, deal.status)
        no_response.keep_writes(session)
        return 409, {"error": "Hot deal has expired"}

    courier = await session.get(Courier, deal.courier_id)
    if courier is not None and courier.user_id == user.id:
        return 403, {"error": "You cannot reserve your own hot deal"}
    # Serializes one buyer's reservations: two at once can't both see 1 running.
    buyer = (await session.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
    if await _active_reservations(session, user.id) >= MAX_ACTIVE_RESERVATIONS:
        return 429, {"error": "too_many_reservations"}

    fee = deal.delivery_fee or Decimal(0)
    if fee <= 0:
        return 500, {"error": "Invalid hot deal delivery fee"}
    lat = _coord(payload.get("delivery_lat"), BUYER_BOUNDS[0], BUYER_BOUNDS[1])
    lng = _coord(payload.get("delivery_lng"), BUYER_BOUNDS[2], BUYER_BOUNDS[3])
    order = Order(
        customer_id=user.id,
        courier_id=deal.courier_id,
        items_text=deal.items_text,
        contact_name=buyer.full_name or "",
        contact_phone_e164=_buyer_phone(payload.get("phone"), buyer.phone_e164),
        delivery_address=address,
        delivery_location=point(lat, lng) if lat is not None and lng is not None else None,
        # what the buyer reimburses: the discounted price (the legacy total = price + fee)
        purchase_amount=deal.price,
        delivery_fee=fee,
        payment_method="cash",
        notes=f"Hot deal reservation ({_js_num_text(deal.discount_percentage)}% off)",
        resale_deal_id=deal.id,
    )
    session.add(order)
    await session.flush()
    if deal.shop_name:
        session.add(
            OrderStop(
                order_id=order.id,
                seq=0,
                name=deal.shop_name,
                address=deal.shop_address,
                location=await _ewkt(session, HotDeal.pickup_location, HotDeal.id == deal.id),
                items=deal.items_text,
                status="purchased",  # the goods are already bought
                purchase_amount=deal.price,
                completed_at=now,
            )
        )
    await ot.start(session, order, user, "reserveHotDeal", status="accepted")

    deal.status, deal.buyer_id, deal.reserved_at, deal.buyer_order_id = "sold", user.id, now, order.id
    await session.flush()
    announce(session, deal, deal.status)

    if courier is not None:
        who_ar = buyer.full_name or "عميل"
        who_fr = buyer.full_name or "Un client"
        await notify_always_pushed(
            session,
            user_id=courier.user_id,
            order_id=order.id,
            type_="hot_deal_reserved",
            title_ar="🔥 تم حجز العرض الساخن",
            title_fr="🔥 Hot deal réservé",
            body_ar=f"{who_ar} حجز العرض: {deal.items_text}. ابدأ التوصيل.",
            body_fr=f"{who_fr} a réservé l'offre : {deal.items_text}. Lancez la livraison.",
            metadata={
                "resale_order_id": str(deal.id),
                "delivery_address": address,
                "recipient_role": "courier",
            },
        )
    return 200, {
        "success": True,
        "order_id": str(order.id),
        "resale_order_id": str(deal.id),
        # the buyer calls the courier from the confirmation screen; listings don't carry it
        "courier_phone": (courier.phone_e164 if courier is not None else None) or "",
    }


# ─────────────────────────── listHotDeals ───────────────────────────


def _photo_url(key: str | None) -> str | None:
    if not key:
        return None
    return key if key.startswith("http") else f"{public_prefix()}{key}"


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def list_deals(session: AsyncSession, payload: dict[str, Any]) -> Result:
    lat, lng = payload.get("lat"), payload.get("lng")
    has_point = is_finite_number(lat) and is_finite_number(lng)
    radius = LIST_RADIUS_DEFAULT if "radius_km" not in payload else payload.get("radius_km")
    radius_km = min(LIST_RADIUS_MAX, max(0, number_or(radius, 0)))
    limit = LIST_LIMIT_DEFAULT if "limit" not in payload else payload.get("limit")
    take = int(min(LIST_LIMIT_MAX, max(1, number_or(limit, LIST_LIMIT_DEFAULT))))
    cursor = js_number(payload.get("cursor")) if "cursor" in payload else math.nan
    offset = int(max(0, cursor)) if math.isfinite(cursor) else 0

    distance: Any = None
    stmt = (
        select(HotDeal, Courier.display_name)
        .join(Courier, Courier.id == HotDeal.courier_id)
        .where(HotDeal.status == "available", HotDeal.expires_at > func.now())
    )
    if has_point:
        # 0.1 km: enough to sort, too coarse to pinpoint the courier.
        meters = func.ST_Distance(HotDeal.pickup_location, func.ST_GeogFromText(point(lat, lng)))
        distance = func.round(cast(meters / 100, Numeric)) / 10
        stmt = stmt.add_columns(distance.label("distance_km")).where(
            or_(HotDeal.pickup_location.is_(None), distance <= radius_km)
        )
        stmt = stmt.order_by(distance.asc().nulls_last(), HotDeal.created_at.desc(), HotDeal.id)
    else:
        stmt = stmt.order_by(HotDeal.created_at.desc(), HotDeal.id)
    rows = (await session.execute(stmt.offset(offset).limit(take + 1))).all()
    deals = []
    for row in rows[:take]:
        deal, courier_name = row[0], row[1]
        km = row[2] if has_point else None
        deals.append(
            {
                "id": str(deal.id),
                "items_text": deal.items_text,
                "shop_name": deal.shop_name,
                "purchase_amount": _number(deal.purchase_amount),
                "discount_percentage": _number(deal.discount_percentage),
                "discounted_price": _number(deal.price),
                "include_delivery": deal.include_delivery,
                "delivery_fee": _number(deal.delivery_fee),
                "photo_url": _photo_url(deal.photo_key),
                "expires_at": legacy_datetime(deal.expires_at),
                "status": deal.status,
                "courier_name": courier_name,
                "created_date": legacy_datetime(deal.created_at),
                "distance_km": float(km) if km is not None else None,
            }
        )
    next_cursor = str(offset + take) if len(rows) > take else None
    return 200, {"success": True, "deals": deals, "total": len(deals), "next_cursor": next_cursor}


# ─────────────────────────── jobs ───────────────────────────


async def expire_deals(session: AsyncSession, now: datetime | None = None) -> int:
    """Listed deals past their expiry → expired (hourly)."""
    now = now or ot.now_utc()
    ids = list(
        (
            await session.execute(
                update(HotDeal)
                .where(HotDeal.status == "available", HotDeal.expires_at <= now)
                .values(status="expired")
                .returning(HotDeal.id)
            )
        ).scalars()
    )
    for deal_id in ids:
        announce(session, deal_id, "expired")
    return len(ids)


async def purge_deals(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """sweepExpiredTestData: expired deals never sold, and QA deals once expired, are deleted
    (a sold deal stays: the buyer's order points at it). Its test-run rows ('test:' ids) can't
    exist here (real FK): the QA deals (items "QA TEST" / "PW-") take their place."""
    now = now or ot.now_utc()
    expired = and_(HotDeal.expires_at < now, HotDeal.status != "sold")
    qa = and_(
        HotDeal.expires_at < now,
        HotDeal.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL),
    )
    due = (
        select(HotDeal.id)
        .where(or_(expired, qa))
        .order_by(HotDeal.expires_at)
        .limit(PURGE_BATCH)
        .with_for_update(skip_locked=True)
    )
    rows = (
        await session.execute(
            delete(HotDeal).where(HotDeal.id.in_(due)).returning(HotDeal.id, expired, qa),
            execution_options={"synchronize_session": False},
        )
    ).all()
    for deal_id, _expired, _qa in rows:
        announce(session, deal_id, "deleted")
    return {
        "expired_deals_deleted": sum(1 for row in rows if row[1]),
        "test_run_deals_deleted": sum(1 for row in rows if row[2]),
    }
