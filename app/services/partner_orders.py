"""Orders created by a partner (restaurant ODS) through /api/partner/v1 — Lot 2, 2A.

The partner's request is turned into the `raw` dict `orders.build_order` validates (the very
same whitelist as placeOrder), the order belongs to the partner's technical user, and:
- `source = 'partner'`, `partner_id`, `partner_ref` (idempotence), `tracking_token`;
- items_text = « [Restaurant ODS] <nom> » + one line per dish + the total; estimated_price =
  budget_max = items_total (the courier knows exactly what he advances at the counter);
- unavailable_policy call_me, payment cash, contact_name = the customer's first name only;
- the partner's `max_open_orders` (default 50) replaces the customer cap of 5;
- `test: true` → « QA TEST » prefix: the classic QA path — offered to the QA accounts only;
- D4: a partner with an unpaid debt gets 409 restaurant_debt_unpaid;
- the answer carries the per-phone badge (app/services/partner_stats.py).
Acceptance and cancellation delegate to offers.accept_offer / cancellation.cancel_order as the
technical user; the views below are the partner's (restaurant) and the public tracking one.
"""

import hashlib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from geoalchemy2 import Geography
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Courier, NoResponseCase, Order, OrderOffer, OrderStop, Partner, User, courier_stats
from app.security.partner import PartnerContext
from app.services import cancellation, launch_mode, partner_stats, tracking
from app.services import order_transitions as ot
from app.services.dispatch import dispatch_order
from app.services.geo import ORDER_BOUNDS, within
from app.services.offers import accept_offer
from app.services.orders import OrderRefused, build_order, is_test_order, tunisian_phone

log = logging.getLogger("odsd.partner_orders")

ITEMS_TEXT_MAX = 2000
NOTES_MAX = 300
TEST_PREFIX = "QA TEST "
LABEL = "[Restaurant ODS]"
# Guard-rail 2 of decision 3 (16/10/2026): the courier calls before paying the deposit at the counter.
COURIER_NOTE_FR = "Appelez le client avant de payer."
COURIER_NOTE_AR = "اتصل بالحريف قبل أن تدفع."
TRACKING_TOKEN_BYTES = 24
AVAILABILITY_RADIUS_M = 10_000

COURIER_STATE = {
    "pending": "searching",
    "offers_received": "searching",
    "accepted": "assigned",
    "at_shop": "at_restaurant",
    "price_confirmation_needed": "at_restaurant",
    "purchased": "picked_up",
    "on_the_way": "on_the_way",
    "delivered": "delivered",
    "client_no_response": "incident_no_response",
    "cancelled": "none",
}


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def courier_state(status: str | None) -> str:
    return COURIER_STATE.get(status or "", "none")


# --- request body ---------------------------------------------------------------------------------


class RestaurantIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    lat: float
    lng: float
    phone: str | None = Field(default=None, max_length=30)
    governorate: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=80)


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200)
    qty: int = Field(default=1, ge=1, le=100)
    unit_price: float | None = Field(default=None, ge=0, le=2000)


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    first_name: str = Field(min_length=1, max_length=60)
    phone: str = Field(min_length=1, max_length=30)


class DropoffIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    address: str = Field(min_length=1, max_length=300)
    details: str | None = Field(default=None, max_length=300)
    lat: float
    lng: float
    governorate: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=80)


class PartnerOrderIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    partner_ref: str | None = Field(default=None, min_length=1, max_length=120)
    test: bool = False
    restaurant: RestaurantIn
    items: list[ItemIn] = Field(min_length=1, max_length=100)
    items_total: float = Field(ge=0, le=2000)
    currency: Literal["TND"] = "TND"
    goods_payment: Literal["courier_advances"] = "courier_advances"
    customer: CustomerIn
    dropoff: DropoffIn
    notes: str | None = Field(default=None, max_length=NOTES_MAX)
    scheduled_for: datetime | None = None

    @field_validator("partner_ref")
    @classmethod
    def _strip_ref(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None


def items_text_of(body: PartnerOrderIn) -> str:
    lines = [f"{LABEL} {body.restaurant.name.strip()}"]
    for item in body.items:
        price = f" — {item.unit_price:.3f} DT" if item.unit_price is not None else ""
        lines.append(f"{item.qty}× {item.name.strip()}{price}")
    lines.append(f"Total repas : {body.items_total:.3f} DT")
    text = "\n".join(lines)
    return TEST_PREFIX + text if body.test else text


def request_hash(body: PartnerOrderIn) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json", exclude={"partner_ref"}), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_tracking_token() -> str:
    return secrets.token_urlsafe(TRACKING_TOKEN_BYTES)


RESALE_LABEL_FR = "Plat vendu en l'état par le livreur"


def resale_items_text(items_text: str | None) -> str:
    """The dishes of a restaurant order as an Offre Chaude shows them: never the restaurant's name
    (decision 10, hygiene — the courier answers for a resold dish, the restaurant does not).
    The « [Restaurant ODS] <nom> » line goes, the QA TEST marker stays."""
    text = items_text or ""
    prefix = ""
    if text.startswith(TEST_PREFIX):
        prefix, text = TEST_PREFIX, text[len(TEST_PREFIX) :]
    lines = [line for line in text.splitlines() if not line.strip().startswith(LABEL)]
    return prefix + "\n".join(lines).strip()


# --- creation -------------------------------------------------------------------------------------


async def open_orders(session: AsyncSession, partner_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.partner_id == partner_id, Order.status.in_(ot.OPEN_STATUSES))
            )
        ).scalar_one()
    )


def _api_error(exc: OrderRefused, code: str | None = None, message: str | None = None) -> ApiError:
    """An OrderRefused as the partner API answers it; its extras (e.g. the order's `status`) travel
    in the body, never as constructor arguments."""
    error = ApiError(exc.status, code or exc.error, message or exc.error.replace("_", " "))
    error.extra = dict(exc.extra)
    return error


def _refused(exc: OrderRefused) -> ApiError:
    messages = {
        "invalid_shop_location": "restaurant.lat/lng must be in Tunisia",
        "invalid_delivery_location": "dropoff.lat/lng must be in Tunisia",
        "invalid_items": "items are required",
        "invalid_shop": "restaurant.name is required",
        "invalid_delivery_address": "dropoff.address is required",
        "phone_required": "customer.phone must be a Tunisian number (8 digits)",
        "phone_unverified": "customer.phone must be a Tunisian number (8 digits)",
    }
    code = "phone_required" if exc.error == "phone_unverified" else exc.error
    return _api_error(exc, code, messages.get(exc.error))


async def create_order(
    session: AsyncSession, ctx: PartnerContext, body: PartnerOrderIn, idempotency_key: str | None
) -> tuple[int, dict[str, Any]]:
    """POST /orders. Returns (201, body) for a new order, (200, body) for a replay."""
    ref = (idempotency_key or "").strip() or None
    if ref and body.partner_ref and ref != body.partner_ref:
        raise ApiError(400, "partner_ref_mismatch", "Idempotency-Key and partner_ref differ")
    ref = ref or body.partner_ref
    if not ref:
        raise ApiError(400, "validation_error", "Idempotency-Key header or partner_ref required")
    if len(ref) > 120:
        raise ApiError(400, "validation_error", "partner_ref: 120 characters at most")
    phone = tunisian_phone(body.customer.phone)
    if phone is None:
        raise ApiError(400, "phone_required", "customer.phone must be a Tunisian number (8 digits)")
    items_text = items_text_of(body)
    if len(items_text) > ITEMS_TEXT_MAX:
        raise ApiError(400, "items_too_long", f"items: {ITEMS_TEXT_MAX} characters at most once written out")
    digest = request_hash(body)

    # The partner row lock serialises its concurrent creations (idempotence + open-order cap).
    partner = (
        await session.execute(select(Partner).where(Partner.id == ctx.partner.id).with_for_update())
    ).scalar_one()
    existing = (
        await session.execute(select(Order).where(Order.partner_id == partner.id, Order.partner_ref == ref))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.partner_request_hash != digest:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Same partner_ref with a different body",
                order_id=str(existing.id),
            )
        return 200, await _created_body(session, existing, dispatched=None, replayed=True)
    if partner.debt_amount and partner.debt_amount > 0:
        raise ApiError(
            409,
            "restaurant_debt_unpaid",
            "A debt to a courier is unpaid: no new delivery until it is settled",
            amount=float(partner.debt_amount),
        )
    if await open_orders(session, partner.id) >= partner.max_open_orders:
        raise ApiError(429, "too_many_open_orders", "Too many open orders", max=partner.max_open_orders)

    user = (await session.execute(select(User).where(User.id == partner.technical_user_id))).scalar_one()
    raw: dict[str, Any] = {
        "items_text": items_text,
        "quantity": sum(item.qty for item in body.items),
        "notes": (body.notes or "").strip(),
        "unavailable_policy": "call_me",
        "estimated_price": body.items_total,
        "budget_max": body.items_total,
        "package_size": "petit",
        "shop_name": body.restaurant.name,
        "shop_address": body.restaurant.address,
        "shop_phone": body.restaurant.phone,
        "shop_governorate": body.restaurant.governorate,
        "shop_city": body.restaurant.city,
        "shop_lat": body.restaurant.lat,
        "shop_lng": body.restaurant.lng,
        "delivery_address": body.dropoff.address,
        "delivery_details": body.dropoff.details,
        "delivery_governorate": body.dropoff.governorate,
        "delivery_city": body.dropoff.city,
        "delivery_lat": body.dropoff.lat,
        "delivery_lng": body.dropoff.lng,
        "customer_phone": phone,
    }
    if body.scheduled_for is not None:
        raw["preferred_time"] = "scheduled"
        raw["scheduled_time"] = body.scheduled_for.isoformat()
    try:
        # The customer's phone is the one the restaurant sent, whatever the technical user's
        # profile holds (order_contact_phone would prefer a Tunisian profile number).
        built = build_order(raw, user, None, None, contact_phone=phone)
    except OrderRefused as exc:
        raise _refused(exc) from exc
    order = built.order
    order.contact_name = body.customer.first_name.strip()[:120]  # the first name only (PLAN §3)
    order.source = "partner"
    order.partner_id = partner.id
    order.partner_ref = ref
    order.partner_request_hash = digest
    order.tracking_token = new_tracking_token()
    order.preferred_courier_id = None
    session.add(order)
    await session.flush()
    session.add_all(built.stops)
    await ot.start(session, order, user.id, "partner_api")
    dispatched: int | None = 0
    # test: true → « QA TEST »: the very path of a classic QA order — offered to the QA accounts only
    # (settings.QA_ACCOUNTS), notifications included; test: false → every courier around.
    try:  # the order exists: a failed broadcast never fails it
        async with session.begin_nested():
            dispatched = (await dispatch_order(session, order)).get("dispatched")
    except Exception:
        log.exception("partner order %s: dispatch failed", order.id)
        dispatched = None
    return 201, await _created_body(session, order, dispatched=dispatched, replayed=False)


async def _created_body(
    session: AsyncSession, order: Order, *, dispatched: int | None, replayed: bool
) -> dict[str, Any]:
    view = await order_view(session, order)
    return {
        "order_id": str(order.id),
        "partner_ref": order.partner_ref,
        "status": order.status,
        "courier_state": courier_state(order.status),
        "tracking_token": order.tracking_token,
        "dispatched": dispatched,
        "replayed": replayed,
        "customer_reliability": view["customer_reliability"],
        "order": view,
    }


# --- views ----------------------------------------------------------------------------------------


async def partner_order(session: AsyncSession, ctx: PartnerContext, order_id: str) -> Order:
    try:
        oid = uuid.UUID(str(order_id))
    except ValueError:
        raise ApiError(404, "order_not_found", "Order not found") from None
    order = await session.get(Order, oid)
    if order is None or order.partner_id != ctx.partner.id:
        raise ApiError(404, "order_not_found", "Order not found")
    return order


async def courier_card(
    session: AsyncSession, courier_id: uuid.UUID | None, *, contact: bool
) -> dict[str, Any] | None:
    """The contract's `courier` object: first name, rating, deliveries, vehicle (+ phone for the
    restaurant when `contact`). None without a courier."""
    if courier_id is None:
        return None
    courier = await session.get(Courier, courier_id)
    if courier is None:
        return None
    card = await tracking.courier_card(session, courier, contact=contact, position=False)
    out = {
        "first_name": card["first_name"],
        "rating": card["average_rating"],
        "total_deliveries": card["total_deliveries"],
        "vehicle": card["vehicle_type"],
    }
    if contact:
        out["phone"] = card["phone"] or None
    return out


_courier = courier_card


async def _no_response(session: AsyncSession, order: Order) -> dict[str, Any] | None:
    if order.status != "client_no_response":
        return None
    case = (
        await session.execute(
            select(NoResponseCase)
            .where(NoResponseCase.order_id == order.id)
            .order_by(NoResponseCase.created_at.desc(), NoResponseCase.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if case is None:
        return {"stage": "waiting", "deadline_at": None, "wait_seconds": None, "return_deadline_at": None}
    from app.services import no_response

    return {
        "stage": case.status,
        "deadline_at": iso(case.deadline_at),
        "wait_seconds": int((case.deadline_at - case.started_at).total_seconds()),
        # D2 (owner, 16/10/2026): the meal must be back at the counter before this time
        "return_deadline_at": iso(await no_response.return_deadline(session, order, case, datetime.now(UTC))),
    }


async def order_view(session: AsyncSession, order: Order) -> dict[str, Any]:
    """The partner's (restaurant's) view: the courier's phone yes, the customer's data it already has."""
    offers_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(OrderOffer)
                .where(OrderOffer.order_id == order.id, OrderOffer.status == "pending")
            )
        ).scalar_one()
    )
    return {
        "order_id": str(order.id),
        "partner_ref": order.partner_ref,
        "status": order.status,
        "courier_state": courier_state(order.status),
        "test": is_test_order(order.items_text),
        "tracking_token": order.tracking_token,
        "items_total": num(order.estimated_price),
        "delivery_fee": num(order.delivery_fee),
        "purchase_amount": num(order.purchase_amount),
        "courier": await _courier(session, order.courier_id, contact=True),
        "eta_minutes": order.eta_minutes,
        "offers_count": offers_count,
        "created_at": iso(order.created_at),
        "accepted_at": iso(order.accepted_at),
        "delivered_at": iso(order.delivered_at),
        "cancelled_at": iso(order.cancelled_at) if order.status == "cancelled" else None,
        "cancelled_by": order.cancelled_by if order.status == "cancelled" else None,
        "cancel_reason": order.cancel_reason if order.status == "cancelled" else None,
        "no_response": await _no_response(session, order),
        "return_outcome": order.partner_return_outcome,
        "customer_reliability": await partner_stats.badge(session, order.contact_phone_e164),
    }


async def offer_view(session: AsyncSession, offer: OrderOffer) -> dict[str, Any]:
    courier = await session.get(Courier, offer.courier_id)
    stats = (
        await session.execute(
            select(courier_stats.c.average_rating, courier_stats.c.total_deliveries).where(
                courier_stats.c.courier_id == offer.courier_id
            )
        )
    ).first()
    return {
        "offer_id": str(offer.id),
        "fee": num(offer.proposed_fee),
        "eta_minutes": offer.eta_minutes,
        "distance_km": num(offer.distance_km),
        "message": offer.message,
        "status": offer.status,
        "created_at": iso(offer.created_at),
        "courier": {
            "first_name": tracking.first_name(courier.display_name) if courier else "",
            "rating": float(stats[0]) if stats and stats[0] is not None else 5,
            "total_deliveries": int(stats[1]) if stats else 0,
            "vehicle": courier.vehicle if courier else None,
        },
    }


async def offers_view(session: AsyncSession, order: Order) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(OrderOffer)
            .where(OrderOffer.order_id == order.id, OrderOffer.status != "withdrawn")
            .order_by((OrderOffer.status != "pending"), OrderOffer.proposed_fee, OrderOffer.created_at)
        )
    ).scalars()
    return [await offer_view(session, offer) for offer in rows]


async def track_view(session: AsyncSession, order: Order) -> dict[str, Any]:
    """GET /track/{token}: no account, no phone, no address, no purchase amount."""
    live = order.status in ot.LIVE_STATUSES
    courier = await session.get(Courier, order.courier_id) if order.courier_id else None
    card = (
        await tracking.courier_card(session, courier, contact=False, position=live, order_id=order.id)
        if courier is not None
        else None
    )
    position = None
    if card is not None and live and card["current_lat"] is not None:
        position = {
            "lat": card["current_lat"],
            "lng": card["current_lng"],
            "updated_at": card["location_updated_at"],
        }
    stop = (
        await session.execute(
            select(OrderStop.name).where(OrderStop.order_id == order.id, OrderStop.seq == 0)
        )
    ).scalar_one_or_none()
    eta = await tracking.ride_eta_minutes(session, order) if live else None
    return {
        "status": order.status,
        "courier_state": courier_state(order.status),
        "restaurant_name": stop,
        "courier": (
            {
                "first_name": card["first_name"],
                "rating": card["average_rating"],
                "vehicle": card["vehicle_type"],
            }
            if card is not None
            else None
        ),
        "position": position,
        "eta_minutes": eta if eta is not None else (order.eta_minutes if live else None),
        "delivery_fee": num(order.delivery_fee),
        "delivered_at": iso(order.delivered_at),
    }


# --- actions --------------------------------------------------------------------------------------


async def accept(
    session: AsyncSession, ctx: PartnerContext, order: Order, offer_id: str, expected_fee: Any = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"order_id": str(order.id), "offer_id": offer_id}
    if expected_fee is not None:
        payload["expected_fee"] = expected_fee
    try:
        order, offer, _courier_user = await accept_offer(session, ctx.user, payload)
    except OrderRefused as exc:
        raise _api_error(exc) from exc
    view = await order_view(session, order)
    return {
        "status": order.status,
        "delivery_fee": num(offer.proposed_fee),
        "courier": view["courier"],
        "order": view,
    }


async def cancel(session: AsyncSession, ctx: PartnerContext, order: Order, reason: str) -> dict[str, Any]:
    if order.status not in cancellation.CUSTOMER_CANCELLABLE:
        error = ApiError(
            409, "cancel_not_allowed", "The courier is at the restaurant or has paid: agree with him"
        )
        error.extra = {"contact": "courier", "status": order.status}
        raise error
    try:
        await cancellation.cancel_order(
            session,
            ctx.user,
            {
                "order_id": str(order.id),
                "reason": reason.strip()[:200] or "partner_cancel",
                "cancelled_by": "customer",
            },
        )
    except OrderRefused as exc:
        error = _api_error(exc, "cancel_not_allowed", str(exc))
        error.status = 409 if exc.status == 400 else exc.status
        raise error from exc
    return {"status": order.status, "order": await order_view(session, order)}


async def availability(session: AsyncSession, lat: float, lng: float) -> dict[str, Any]:
    """GET /availability: dispatch.py's candidates around a point, without notifying anyone."""
    offline_too = await launch_mode.notify_offline_couriers(session)
    if not within(lat, lng, ORDER_BOUNDS):
        return {
            "serves": False,
            "couriers_free_within_10km": 0,
            "couriers_verified_within_10km": 0,
            "launch_mode": offline_too,
        }
    point = cast(func.ST_MakePoint(lng, lat), Geography(srid=4326))
    near = func.ST_DWithin(Courier.last_location, point, AVAILABILITY_RADIUS_M)
    busy = exists().where(
        Order.courier_id == Courier.id, Order.status.in_(("accepted", "at_shop", "purchased", "on_the_way"))
    )
    base = [
        Courier.verification == "verified",
        Courier.last_location.is_not(None),
        near,
        User.deleted_at.is_(None),
    ]
    free = [~busy] + ([] if offline_too else [Courier.is_online.is_(True)])
    verified_count, free_count = (
        await session.execute(
            select(func.count(), func.count().filter(*free))
            .select_from(Courier)
            .join(User, User.id == Courier.user_id)
            .where(*base)
        )
    ).one()
    return {
        "serves": int(verified_count) > 0,
        "couriers_free_within_10km": int(free_count),
        "couriers_verified_within_10km": int(verified_count),
        "launch_mode": offline_too,
    }
