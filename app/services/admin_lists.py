"""Admin, read only (owner 20/10): the Commandes / Livreurs / Utilisateurs tabs, filtered, sorted and
paginated by the server — the browser no longer loads every row to filter it.

Nothing here changes a row; every function refuses a non-admin caller (403). « Masquer les comptes et
commandes de test » (`hide_tests`, on unless the payload says false) uses the same rules as
admin_sheets: the test accounts (_test_user_sql) and the « QA TEST » / « PW- » orders.

Periods are Tunis calendar days: `period` today | 7d | 30d, or `date_from` / `date_to` (YYYY-MM-DD,
both included). Pages: `page` from 1, `page_size` up to 100.
"""

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Numeric,
    Text,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    literal,
    not_,
    or_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import (
    Courier,
    CourierLedgerEntry,
    NoResponseCase,
    Order,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStop,
    Partner,
    RefreshToken,
    Shop,
    User,
)
from app.security.deps import CurrentUser
from app.services.admin_sheets import _forbidden, _money, _test_user_sql
from app.services.commission import COMMISSION_KINDS, KIND_DUE, LEGACY_STATUS
from app.services.credit import BALANCE_KINDS
from app.services.orders import TEST_ORDER_SQL, is_test_order
from app.services.super_admin import is_super_admin_email

Result = tuple[int, dict[str, Any]]
TUNIS = ZoneInfo("Africa/Tunis")
PAGE_MAX = 100
PAGE_DEFAULT = 50

# the owner's words → the order statuses (« incident »: the customer did not answer)
STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "waiting": ("pending", "awaiting_restaurant"),
    "offers": ("offers_received",),
    "in_progress": (
        "accepted",
        "at_shop",
        "price_confirmation_needed",
        "purchased",
        "on_the_way",
        "preparing",
        "ready",
    ),
    "delivered": ("delivered", "collected"),
    "cancelled": ("cancelled",),
    "incident": ("client_no_response",),
}
DONE = ("delivered", "collected")
ORDER_TYPES = ("free", "vitrine", "resale", "pickup", "partner")


# ------------------------------------------------------------------ payload helpers


def _text(payload: dict[str, Any], key: str, limit: int = 120) -> str:
    value = payload.get(key)
    return str(value).strip()[:limit] if isinstance(value, str | int | float) else ""


def _like(value: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", value) + "%"


def _ilike(column: Any, value: str) -> ColumnElement[bool]:
    return column.ilike(_like(value), escape="\\")


def _number(payload: dict[str, Any], key: str) -> Decimal | None:
    raw = payload.get(key)
    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).replace(",", "."))
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _bool(payload: dict[str, Any], key: str) -> bool | None:
    raw = payload.get(key)
    if isinstance(raw, bool):
        return raw
    if raw in ("yes", "true", "1", 1):
        return True
    if raw in ("no", "false", "0", 0):
        return False
    return None


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except (TypeError, ValueError):
        return None


def _tunis_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=TUNIS).astimezone(UTC)


def _period(payload: dict[str, Any], column: Any, now: datetime) -> list[ColumnElement[bool]]:
    """today | 7d | 30d (rolling), or date_from / date_to (Tunis days, both included)."""
    period = _text(payload, "period")
    if period == "today":
        return [column >= _tunis_midnight(now.astimezone(TUNIS).date())]
    if period in ("7d", "30d"):
        return [column >= now - timedelta(days=int(period[:-1]))]
    out = []
    if (start := _day(payload.get("date_from"))) is not None:
        out.append(column >= _tunis_midnight(start))
    if (end := _day(payload.get("date_to"))) is not None:
        out.append(column < _tunis_midnight(end + timedelta(days=1)))
    return out


def _page(payload: dict[str, Any]) -> tuple[int, int]:
    try:
        page = max(1, int(payload.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(payload.get("page_size") or PAGE_DEFAULT)
    except (TypeError, ValueError):
        size = PAGE_DEFAULT
    return page, min(PAGE_MAX, max(1, size))


def _hide_tests(payload: dict[str, Any]) -> bool:
    return payload.get("hide_tests") is not False


def _is_test(users: Any) -> ColumnElement[bool]:
    """A test account; false (never NULL) when the outer-joined account is missing."""
    return func.coalesce(_test_user_sql(users), false())


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ Commandes


def _cart_total() -> Any:
    raw = Order.cart["items_total"].astext
    return case(
        (raw.op("~", return_type=Boolean)(r"^[0-9]+(\.[0-9]+)?$"), raw.cast(Numeric(10, 3))), else_=None
    )


def _order_type() -> Any:
    return case(
        (Order.source == "partner", literal("partner")),
        (Order.fulfillment == "pickup", literal("pickup")),
        (Order.source == "vitrine", literal("vitrine")),
        (Order.resale_deal_id.is_not(None), literal("resale")),
        else_=literal("free"),
    )


async def list_orders(session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any]) -> Result:
    if (refused := _forbidden(user)) is not None:
        return refused
    now = _now()
    customer = User.__table__.alias("customer")
    courier_account = User.__table__.alias("courier_account")
    vshop = Shop.__table__.alias("vshop")
    items_amount = func.coalesce(Order.purchase_amount, _cart_total(), Order.estimated_price)
    total_amount = items_amount + func.coalesce(Order.delivery_fee, 0)
    has_case = exists().where(NoResponseCase.order_id == Order.id)
    has_issue = exists().where(OrderIssue.order_id == Order.id)
    has_incident = or_(has_case, has_issue, Order.status == "client_no_response")

    conds: list[Any] = []
    if _hide_tests(payload):
        conds.append(
            not_(
                or_(
                    Order.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL),
                    _is_test(customer.c),
                    _is_test(courier_account.c),
                )
            )
        )
    statuses = (
        [s for s in (payload.get("status") or []) if s in STATUS_GROUPS]
        if isinstance(payload.get("status"), list)
        else ([payload["status"]] if payload.get("status") in STATUS_GROUPS else [])
    )
    if statuses:
        conds.append(Order.status.in_([st for g in statuses for st in STATUS_GROUPS[g]]))
    conds += _period(payload, Order.created_at, now)
    if (kind := _text(payload, "type")) in ORDER_TYPES:
        conds.append(_order_type() == kind)
    if client := _text(payload, "customer"):
        cid = _uuid_or_none(client)
        conds.append(
            customer.c.id == cid
            if cid
            else or_(
                _ilike(customer.c.full_name, client),
                _ilike(customer.c.email, client),
                _ilike(customer.c.phone_e164, client.replace(" ", "")),
                _ilike(Order.contact_name, client),
            )
        )
    if courier_q := _text(payload, "courier"):
        kid = _uuid_or_none(courier_q)
        conds.append(
            Courier.id == kid
            if kid
            else or_(
                _ilike(Courier.display_name, courier_q),
                _ilike(Courier.phone_e164, courier_q.replace(" ", "")),
            )
        )
    if _bool(payload, "no_courier"):
        conds.append(Order.courier_id.is_(None))
    if shop := _text(payload, "shop"):
        conds.append(
            or_(
                exists().where(OrderStop.order_id == Order.id, _ilike(OrderStop.name, shop)),
                _ilike(vshop.c.name, shop),
            )
        )
    if (low := _number(payload, "amount_min")) is not None:
        conds.append(total_amount >= low)
    if (high := _number(payload, "amount_max")) is not None:
        conds.append(total_amount <= high)
    incident = _bool(payload, "incident")
    if incident is True:
        conds.append(has_incident)
    elif incident is False:
        conds.append(not_(has_incident))
    if q := _text(payload, "q"):
        digits = q.replace(" ", "")
        conds.append(
            or_(
                cast(Order.id, Text).ilike(q.lower().lstrip("#") + "%"),
                _ilike(Order.items_text, q),
                _ilike(Order.notes, q),
                _ilike(Order.delivery_address, q),
                _ilike(Order.delivery_city, q),
                _ilike(Order.contact_name, q),
                _ilike(Order.contact_phone_e164, digits),
                _ilike(customer.c.full_name, q),
                _ilike(customer.c.email, q),
                _ilike(customer.c.phone_e164, digits),
                _ilike(Courier.display_name, q),
                _ilike(Courier.phone_e164, digits),
                _ilike(vshop.c.name, q),
                exists().where(
                    OrderStop.order_id == Order.id, or_(_ilike(OrderStop.name, q), _ilike(OrderStop.items, q))
                ),
            )
        )

    def joined(stmt: Any) -> Any:
        return (
            stmt.select_from(Order)
            .join(customer, customer.c.id == Order.customer_id)
            .outerjoin(Courier, Courier.id == Order.courier_id)
            .outerjoin(courier_account, courier_account.c.id == Courier.user_id)
            .outerjoin(vshop, vshop.c.id == Order.vitrine_shop_id)
            .where(and_(true(), *conds))
        )

    count, items_sum, fees_sum, delivered_count = (
        await session.execute(
            joined(
                select(
                    func.count(),
                    func.coalesce(func.sum(case((Order.status.in_(DONE), items_amount))), 0),
                    func.coalesce(func.sum(case((Order.status.in_(DONE), Order.delivery_fee))), 0),
                    func.count().filter(Order.status.in_(DONE)),
                )
            )
        )
    ).one()
    selected = joined(select(Order.id)).subquery()
    commission_due, commission_offered = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(CourierLedgerEntry.amount).filter(CourierLedgerEntry.kind == KIND_DUE), 0
                ),
                func.coalesce(
                    func.sum(CourierLedgerEntry.amount).filter(CourierLedgerEntry.kind != KIND_DUE), 0
                ),
            ).where(
                CourierLedgerEntry.order_id.in_(select(selected.c.id)),
                CourierLedgerEntry.kind.in_(COMMISSION_KINDS),
            )
        )
    ).one()

    sort = _text(payload, "sort")
    order_by = {
        "oldest": [Order.created_at.asc()],
        "amount_desc": [total_amount.desc().nulls_last(), Order.created_at.desc()],
        "amount_asc": [total_amount.asc().nulls_last(), Order.created_at.desc()],
    }.get(sort, [Order.created_at.desc()])
    page, size = _page(payload)
    offers_count = (
        select(func.count()).where(OrderOffer.order_id == Order.id).correlate(Order).scalar_subquery()
    )
    rows = (
        await session.execute(
            joined(
                select(
                    Order,
                    customer.c.full_name,
                    customer.c.email,
                    customer.c.phone_e164,
                    _is_test(customer.c).label("test_customer"),
                    Courier.display_name,
                    Courier.phone_e164,
                    _is_test(courier_account.c).label("test_courier"),
                    vshop.c.name,
                    _order_type().label("kind"),
                    items_amount.label("items_amount"),
                    offers_count.label("offers_count"),
                )
            )
            .order_by(*order_by)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    ids = [r[0].id for r in rows]
    stops: dict[uuid.UUID, list[dict[str, Any]]] = {}
    commissions: dict[uuid.UUID, tuple[str, Decimal]] = {}
    incidents: dict[uuid.UUID, list[dict[str, Any]]] = {}
    partners: dict[uuid.UUID, str] = {}
    if ids:
        for st in (
            await session.execute(
                select(OrderStop).where(OrderStop.order_id.in_(ids)).order_by(OrderStop.seq)
            )
        ).scalars():
            stops.setdefault(st.order_id, []).append(
                {
                    "name": st.name,
                    "address": st.address,
                    "city": st.city,
                    "items": st.items,
                    "purchase_amount": _money(st.purchase_amount),
                    "status": st.status,
                }
            )
        for oid, kind, amount in (
            await session.execute(
                select(CourierLedgerEntry.order_id, CourierLedgerEntry.kind, CourierLedgerEntry.amount).where(
                    CourierLedgerEntry.order_id.in_(ids), CourierLedgerEntry.kind.in_(COMMISSION_KINDS)
                )
            )
        ).all():
            commissions[oid] = (kind, amount)
        for c in (
            await session.execute(select(NoResponseCase).where(NoResponseCase.order_id.in_(ids)))
        ).scalars():
            incidents.setdefault(c.order_id, []).append(
                {
                    "kind": "no_response",
                    "date": legacy_datetime(c.started_at),
                    "status": c.status,
                    "resolution": c.resolution,
                    "counted": bool(c.incident_counted),
                }
            )
        for i in (await session.execute(select(OrderIssue).where(OrderIssue.order_id.in_(ids)))).scalars():
            incidents.setdefault(i.order_id, []).append(
                {
                    "kind": "issue",
                    "date": legacy_datetime(i.created_at),
                    "issue_type": i.issue_type,
                    "description": (i.description or "")[:300] or None,
                    "resolved": i.resolved_at is not None,
                }
            )
        partner_ids = {r[0].partner_id for r in rows if r[0].partner_id}
        if partner_ids:
            partners = dict(
                (
                    await session.execute(select(Partner.id, Partner.name).where(Partner.id.in_(partner_ids)))
                ).all()
            )

    out = []
    for (
        o,
        c_name,
        c_email,
        c_phone,
        test_customer,
        k_name,
        k_phone,
        test_courier,
        vshop_name,
        kind,
        items_value,
        n_offers,
    ) in rows:
        fee = o.delivery_fee
        commission_kind, commission_amount = commissions.get(o.id, (None, None))
        cart = o.cart if isinstance(o.cart, dict) else None
        out.append(
            {
                "id": str(o.id),
                "created_date": legacy_datetime(o.created_at),
                "accepted_at": legacy_datetime(o.accepted_at),
                "delivered_at": legacy_datetime(o.delivered_at or o.collected_at),
                "cancelled_at": legacy_datetime(o.cancelled_at),
                "scheduled_for": legacy_datetime(o.scheduled_for),
                "status": str(o.status),
                "type": kind,
                "fulfillment": o.fulfillment,
                "source": o.source,
                "items_text": o.items_text,
                "quantity": o.quantity,
                "notes": o.notes,
                "alternatives": o.alternatives,
                "unavailable_policy": o.unavailable_policy,
                "package": o.package,
                "vitrine_cart": {
                    "items_total": cart.get("items_total"),
                    "lines": [
                        {k: v for k, v in line.items() if k != "ods_menu_id"}
                        for line in cart.get("lines") or []
                        if isinstance(line, dict)
                    ],
                }
                if cart
                else None,
                "stops": stops.get(o.id, []),
                "vitrine_shop": vshop_name,
                "partner_name": partners.get(o.partner_id) if o.partner_id else None,
                "pickup_code": o.pickup_code,
                "delivery_address": o.delivery_address,
                "delivery_details": o.delivery_details,
                "delivery_city": o.delivery_city,
                "delivery_governorate": o.delivery_governorate,
                "distance_km": _money(o.distance_km),
                "customer": {
                    "id": str(o.customer_id),
                    "name": c_name or o.contact_name,
                    "email": c_email,
                    "phone": c_phone,
                },
                "contact_name": o.contact_name,
                "contact_phone": o.contact_phone_e164,
                "courier": {"id": str(o.courier_id), "name": k_name, "phone": k_phone}
                if o.courier_id
                else None,
                "items_amount": _money(items_value),
                "purchase_amount": _money(o.purchase_amount),
                "estimated_price": _money(o.estimated_price),
                "budget_max": _money(o.budget_max),
                "delivery_fee": _money(fee),
                "total_amount": None if items_value is None else _money(items_value + (fee or 0)),
                "commission": _money(commission_amount),
                "commission_status": LEGACY_STATUS.get(commission_kind) if commission_kind else None,
                "payment_method": o.payment_method,
                "offers_count": int(n_offers or 0),
                "incidents": sorted(incidents.get(o.id, []), key=lambda r: r["date"] or "", reverse=True),
                "cancelled_by": o.cancelled_by,
                "cancel_reason": o.cancel_reason,
                "late_minutes": o.punctuality_late_minutes,
                "is_test_order": is_test_order(o.items_text) or bool(test_customer) or bool(test_courier),
            }
        )
    return 200, {
        "success": True,
        "orders": out,
        "page": page,
        "page_size": size,
        "total": int(count),
        "totals": {
            "orders": int(count),
            "delivered": int(delivered_count),
            "items": _money(Decimal(items_sum)),
            "fees": _money(Decimal(fees_sum)),
            "commission_due": _money(Decimal(commission_due)),
            "commission_offered": _money(Decimal(commission_offered)),
        },
    }


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# ------------------------------------------------------------------ Livreurs


async def list_couriers(session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any]) -> Result:
    if (refused := _forbidden(user)) is not None:
        return refused
    now = _now()
    account = User.__table__.alias("account")
    since30 = now - timedelta(days=30)
    done = (
        select(
            Order.courier_id.label("courier_id"),
            func.count().label("deliveries"),
            func.count()
            .filter(func.coalesce(Order.delivered_at, Order.collected_at) >= since30)
            .label("d30"),
            func.coalesce(func.sum(Order.delivery_fee), 0).label("fees"),
            func.max(func.coalesce(Order.delivered_at, Order.collected_at)).label("last_delivery"),
        )
        .where(Order.courier_id.is_not(None), Order.status.in_(DONE))
        .group_by(Order.courier_id)
        .subquery("done")
    )
    ledger = (
        select(
            CourierLedgerEntry.courier_id.label("courier_id"),
            func.coalesce(
                func.sum(CourierLedgerEntry.amount).filter(CourierLedgerEntry.kind == KIND_DUE), 0
            ).label("due"),
            func.coalesce(
                func.sum(CourierLedgerEntry.amount).filter(CourierLedgerEntry.kind.in_(BALANCE_KINDS)), 0
            ).label("balance"),
        )
        .group_by(CourierLedgerEntry.courier_id)
        .subquery("ledger")
    )
    ratings = (
        select(
            OrderRating.courier_id.label("courier_id"),
            func.avg(OrderRating.rating).label("avg"),
            func.count().label("n"),
        )
        .group_by(OrderRating.courier_id)
        .subquery("ratings")
    )
    invited = (
        select(User.referred_by_courier_id.label("courier_id"), func.count().label("n"))
        .where(User.referred_by_courier_id.is_not(None), User.deleted_at.is_(None))
        .group_by(User.referred_by_courier_id)
        .subquery("invited")
    )
    deliveries = func.coalesce(done.c.deliveries, 0)
    activity = func.coalesce(Courier.last_seen_at, Courier.online_since, done.c.last_delivery)

    conds: list[Any] = [account.c.deleted_at.is_(None)]
    if _hide_tests(payload):
        conds.append(not_(_is_test(account.c)))
    if (verif := _text(payload, "verification")) in ("pending", "verified", "rejected"):
        conds.append(Courier.verification == verif)
    if (online := _bool(payload, "online")) is not None:
        conds.append(Courier.is_online.is_(online))
    if (vehicle := _text(payload, "vehicle")) in ("walking", "scooter", "car"):
        conds.append(Courier.vehicle == vehicle)
    conds += _period(payload, Courier.created_at, now)
    if (active := _text(payload, "active")) in ("7d", "30d"):
        conds.append(activity >= now - timedelta(days=int(active[:-1])))
    elif active == "inactive30":
        conds.append(or_(activity.is_(None), activity < now - timedelta(days=30)))
    if _bool(payload, "late_cancellations"):
        conds.append(Courier.late_cancellations > 0)
    if q := _text(payload, "q"):
        digits = q.replace(" ", "")
        conds.append(
            or_(
                _ilike(Courier.display_name, q),
                _ilike(Courier.phone_e164, digits),
                _ilike(account.c.email, q),
                _ilike(account.c.full_name, q),
                _ilike(Courier.vehicle_plate, q),
                _ilike(func.replace(Courier.vehicle_plate, " ", ""), digits),
                _ilike(Courier.service_city, q),
                _ilike(Courier.service_governorate, q),
            )
        )

    def joined(stmt: Any) -> Any:
        return (
            stmt.select_from(Courier)
            .join(account, account.c.id == Courier.user_id)
            .outerjoin(done, done.c.courier_id == Courier.id)
            .outerjoin(ledger, ledger.c.courier_id == Courier.id)
            .outerjoin(ratings, ratings.c.courier_id == Courier.id)
            .outerjoin(invited, invited.c.courier_id == Courier.id)
            .where(and_(*conds))
        )

    count, online_count, pending_count = (
        await session.execute(
            joined(
                select(
                    func.count(),
                    func.count().filter(Courier.is_online.is_(True)),
                    func.count().filter(Courier.verification == "pending"),
                )
            )
        )
    ).one()
    sort = _text(payload, "sort")
    order_by = {
        "deliveries": [deliveries.desc(), Courier.created_at.desc()],
        "rating": [ratings.c.avg.desc().nulls_last(), Courier.created_at.desc()],
        "activity": [activity.desc().nulls_last(), Courier.created_at.desc()],
    }.get(sort, [Courier.created_at.desc()])
    page, size = _page(payload)
    rows = (
        await session.execute(
            joined(
                select(
                    Courier,
                    account.c.email,
                    account.c.disabled_at,
                    _is_test(account.c).label("test"),
                    deliveries.label("deliveries"),
                    func.coalesce(done.c.d30, 0),
                    func.coalesce(done.c.fees, 0),
                    func.coalesce(ledger.c.due, 0),
                    func.coalesce(ledger.c.balance, 0),
                    ratings.c.avg,
                    func.coalesce(ratings.c.n, 0),
                    func.coalesce(invited.c.n, 0),
                    activity.label("activity"),
                )
            )
            .order_by(*order_by)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    out = [
        {
            "id": str(c.id),
            "user_id": str(c.user_id),
            "email": email,
            "full_name": c.display_name,
            "phone": c.phone_e164,
            "verification_status": c.verification,
            "is_online": bool(c.is_online),
            "last_seen_at": legacy_datetime(last_activity),
            "vehicle_type": c.vehicle,
            "vehicle_model": c.vehicle_model,
            "vehicle_plate": c.vehicle_plate,
            "price_per_km": _money(c.price_per_km),
            "min_fee": _money(c.min_fee),
            "city": c.service_city,
            "governorate": c.service_governorate,
            "created_date": legacy_datetime(c.created_at),
            "average_rating": round(float(avg), 2) if avg is not None else None,
            "ratings_count": int(n_ratings),
            "deliveries": int(n_done),
            "deliveries_30d": int(n30),
            "fees": _money(Decimal(fees)),
            "earnings": _money(Decimal(fees) - Decimal(due)),
            "commission_due_total": _money(Decimal(due)),
            "credit_balance": _money(-Decimal(balance)),
            "late_cancellations": c.late_cancellations,
            "invited_clients": int(n_invited),
            "is_active": disabled is None,
            "is_test_account": bool(test),
        }
        for (
            c,
            email,
            disabled,
            test,
            n_done,
            n30,
            fees,
            due,
            balance,
            avg,
            n_ratings,
            n_invited,
            last_activity,
        ) in rows
    ]
    return 200, {
        "success": True,
        "couriers": out,
        "page": page,
        "page_size": size,
        "total": int(count),
        "totals": {"couriers": int(count), "online": int(online_count), "pending": int(pending_count)},
    }


# ------------------------------------------------------------------ Utilisateurs


async def list_users(session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any]) -> Result:
    if (refused := _forbidden(user)) is not None:
        return refused
    now = _now()
    inviter = Courier.__table__.alias("inviter")
    own_courier = Courier.__table__.alias("own_courier")
    # « Masquer … de test » (QA 20/10 d6-admin-2): a user's orders, spent and « Ont commandé » leave out the
    # test orders the Commandes list hides (« QA TEST » / « PW- », delivered by a test courier) — the partner
    # account « Coffee and Tea » read 6 orders / 2 delivered while Commandes showed none of them
    order_conds: list[Any] = []
    if _hide_tests(payload):
        courier_user = User.__table__.alias("placed_courier_user")
        by_test_courier = (
            select(Courier.id)
            .join(courier_user, courier_user.c.id == Courier.user_id)
            .where(Courier.id == Order.courier_id, _is_test(courier_user.c))
            .exists()
        )
        order_conds = [
            not_(func.coalesce(Order.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL), false())),
            not_(by_test_courier),
        ]
    placed = (
        select(
            Order.customer_id.label("user_id"),
            func.count().label("orders"),
            func.count().filter(Order.status.in_(DONE)).label("done"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Order.status.in_(DONE),
                            func.coalesce(Order.purchase_amount, 0) + func.coalesce(Order.delivery_fee, 0),
                        )
                    )
                ),
                0,
            ).label("spent"),
            func.max(Order.created_at).label("last_order"),
        )
        .where(*order_conds)
        .group_by(Order.customer_id)
        .subquery("placed")
    )
    unpaid = (
        select(Order.customer_id.label("user_id"), func.count(func.distinct(Order.id)).label("n"))
        .join(NoResponseCase, NoResponseCase.order_id == Order.id)
        .where(NoResponseCase.incident_counted.is_(True), *order_conds)
        .group_by(Order.customer_id)
        .subquery("unpaid")
    )
    sessions = (
        select(
            RefreshToken.user_id.label("user_id"),
            func.max(func.greatest(RefreshToken.created_at, RefreshToken.rotated_at)).label("last"),
        )
        .group_by(RefreshToken.user_id)
        .subquery("sessions")
    )
    incident_any = or_(
        exists().where(NoResponseCase.order_id == Order.id, Order.customer_id == User.id),
        exists().where(OrderIssue.order_id == Order.id, Order.customer_id == User.id),
    )
    orders_n = func.coalesce(placed.c.orders, 0)
    spent = func.coalesce(placed.c.spent, 0)
    activity = func.greatest(sessions.c.last, placed.c.last_order, User.created_at)

    conds: list[Any] = [User.deleted_at.is_(None)]
    if _hide_tests(payload):
        conds.append(not_(_is_test(User)))
    role = _text(payload, "role")
    if role == "both":
        conds.append(and_(User.role == "customer", own_courier.c.id.is_not(None)))
    elif role == "courier":
        conds.append(User.role == "courier")
    elif role == "customer":
        conds.append(and_(User.role == "customer", own_courier.c.id.is_(None)))
    elif role == "admin":
        conds.append(User.role == "admin")
    conds += _period(payload, User.created_at, now)
    if (active := _text(payload, "active")) in ("7d", "30d"):
        conds.append(activity >= now - timedelta(days=int(active[:-1])))
    if (with_orders := _bool(payload, "has_orders")) is True:
        conds.append(orders_n > 0)
    elif with_orders is False:
        conds.append(orders_n == 0)
    if _bool(payload, "incidents"):
        conds.append(incident_any)
    if _bool(payload, "invited"):
        conds.append(User.referred_by_courier_id.is_not(None))
    if (signup := _text(payload, "signup")) == "google":
        conds.append(User.google_sub.is_not(None))
    elif signup == "email":
        conds.append(User.google_sub.is_(None))
    if (state := _text(payload, "state")) == "disabled":
        conds.append(User.disabled_at.is_not(None))
    elif state == "active":
        conds.append(User.disabled_at.is_(None))
    if q := _text(payload, "q"):
        conds.append(
            or_(
                _ilike(User.full_name, q),
                _ilike(User.email, q),
                _ilike(User.phone_e164, q.replace(" ", "")),
                _ilike(inviter.c.display_name, q),
            )
        )

    def joined(stmt: Any) -> Any:
        return (
            stmt.select_from(User)
            .outerjoin(placed, placed.c.user_id == User.id)
            .outerjoin(unpaid, unpaid.c.user_id == User.id)
            .outerjoin(sessions, sessions.c.user_id == User.id)
            .outerjoin(inviter, inviter.c.id == User.referred_by_courier_id)
            .outerjoin(own_courier, own_courier.c.user_id == User.id)
            .where(and_(*conds))
        )

    count, with_orders_n = (
        await session.execute(joined(select(func.count(), func.count().filter(orders_n > 0))))
    ).one()
    sort = _text(payload, "sort")
    order_by = {
        "orders": [orders_n.desc(), User.created_at.desc()],
        "spent": [spent.desc(), User.created_at.desc()],
        "activity": [activity.desc().nulls_last(), User.created_at.desc()],
        "oldest": [User.created_at.asc()],
    }.get(sort, [User.created_at.desc()])
    page, size = _page(payload)
    rows = (
        await session.execute(
            joined(
                select(
                    User,
                    _is_test(User).label("test"),
                    orders_n,
                    func.coalesce(placed.c.done, 0),
                    spent,
                    func.coalesce(unpaid.c.n, 0),
                    activity,
                    inviter.c.id,
                    inviter.c.display_name,
                    own_courier.c.id,
                )
            )
            .order_by(*order_by)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    out = [
        {
            "id": str(u.id),
            "user_id": u.email,  # the legacy lists' key (UserProfile.user_id)
            "email": u.email,
            "full_name": u.full_name,
            "phone": u.phone_e164,
            "role": "both" if (courier_id and u.role == "customer") else str(u.role),
            "courier_id": str(courier_id) if courier_id else None,
            "language": u.language,
            "created_date": legacy_datetime(u.created_at),
            "last_activity": legacy_datetime(last),
            "orders": int(n),
            "delivered": int(n_done),
            "spent": _money(Decimal(total)),
            "unpaid": int(n_unpaid),
            "invited_by": {"id": str(inv_id), "name": inv_name} if inv_id else None,
            "signup": "google" if u.google_sub else "email",
            "is_active": u.disabled_at is None,
            "is_test_account": bool(test),
            "is_super_admin": is_super_admin_email(u.email) and u.role == "admin",
        }
        for u, test, n, n_done, total, n_unpaid, last, inv_id, inv_name, courier_id in rows
    ]
    return 200, {
        "success": True,
        "users": out,
        "page": page,
        "page_size": size,
        "total": int(count),
        "totals": {"users": int(count), "with_orders": int(with_orders_n)},
    }
