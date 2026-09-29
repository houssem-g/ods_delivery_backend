"""Order: `orders` + its stops, status events, live position, rating, issues, no-response case
and commission entry, in the legacy document shape (docs/FIELD_MAPPING.md, Order).

Read (base44/entities/Order.jsonc): the customer, the assigned courier, admins; open orders
(pending / offers_received) also to every VERIFIED courier, to bid — except QA orders
("QA TEST" / "PW-"), shown to the QA accounts only (src/lib/orderUtils.js). Field rules:
customer_phone, delivery_details, courier_live_*, reported_issues, has_issues, stock_check(s)
only for the order's parties and admins.
Write: no create (placeOrder) and no delete; update = the assigned courier's delivery steps
and the customer's geocode (app/services/order_steps.py); anything else 403.
"""

from typing import Any

from sqlalchemy import Boolean, Text, and_, case, cast, exists, func, literal, not_, null, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.query import get_document
from app.compat.registry import EntityDef, LegacyField, register
from app.config import settings
from app.errors import ApiError
from app.models import (
    Courier,
    CourierLedgerEntry,
    NoResponseCase,
    OfferIntent,
    Order,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStatusEvent,
    OrderStockCheck,
    OrderStop,
    OrderTracking,
    User,
    courier_stats,
)
from app.security.deps import CurrentUser
from app.services import order_steps
from app.services import order_transitions as ot
from app.services.commission import COMMISSION_KINDS, LEGACY_STATUS
from app.services.geo import lat_of, lng_of
from app.services.offer_intents import INTENT_TTL
from app.services.orders import TEST_ORDER_SQL, is_qa_account

orders = Order.__table__
customer = User.__table__.alias("order_customer")
courier = Courier.__table__.alias("order_courier")
courier_user = User.__table__.alias("order_courier_user")
stop0 = OrderStop.__table__.alias("order_stop0")
live = OrderTracking.__table__.alias("order_live")
rating = OrderRating.__table__.alias("order_rating")
couriers = Courier.__table__
users = User.__table__
events = OrderStatusEvent.__table__
stops = OrderStop.__table__
issues = OrderIssue.__table__
cases = NoResponseCase.__table__
checks = OrderStockCheck.__table__
ledger = CourierLedgerEntry.__table__

latest_case = (
    select(cases)
    .where(cases.c.order_id == orders.c.id)
    .order_by(cases.c.created_at.desc(), cases.c.id.desc())
    .limit(1)
    .lateral("order_case")
)
commission = (
    select(ledger.c.kind, ledger.c.amount)
    .where(ledger.c.order_id == orders.c.id, ledger.c.kind.in_(COMMISSION_KINDS))
    .limit(1)
    .lateral("order_commission")
)
EMPTY_ARRAY = cast(literal("[]"), JSONB)


def _iso_z(column: Any) -> Any:
    """ISO text with Z, like the timestamps the app wrote into status_history / shops."""
    return func.to_char(func.timezone("UTC", column), 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')


def _public_url(key: Any) -> Any:
    return case((key.like("public/%"), literal(f"{settings.public_files_base_url}/") + key), else_=null())


def my_courier_ids(user: CurrentUser) -> Any:
    return select(couriers.c.id).where(couriers.c.user_id == user.id)


def parties(user: CurrentUser) -> Any:
    return or_(orders.c.customer_id == user.id, orders.c.courier_id.in_(my_courier_ids(user)))


def party_or_admin(user: CurrentUser) -> Any:
    return true() if user.is_admin else parties(user)


def read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    verified_courier = exists().where(couriers.c.user_id == user.id, couriers.c.verification == "verified")
    not_qa = (
        true()
        if is_qa_account(user.email)
        else not_(orders.c.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL))
    )
    open_to_bid = and_(orders.c.status.in_(ot.OPEN_STATUSES), verified_courier, not_qa)
    return or_(parties(user), open_to_bid)


def _history() -> Any:
    entry = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            "status", cast(events.c.to_status, Text),
            "timestamp", _iso_z(events.c.created_at),
            "source", events.c.source,
            "lat", lat_of(events.c.location),
            "lng", lng_of(events.c.location),
            "cancelled_by", events.c.cancelled_by,
            "reason", events.c.reason,
        )
    )  # fmt: skip
    agg = func.jsonb_agg(aggregate_order_by(entry, events.c.created_at, events.c.id))
    return select(func.coalesce(agg, EMPTY_ARRAY)).where(events.c.order_id == orders.c.id).scalar_subquery()


def _shops() -> Any:
    entry = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            "name", stops.c.name,
            "address", stops.c.address,
            "lat", lat_of(stops.c.location),
            "lng", lng_of(stops.c.location),
            "items", stops.c["items"],
            "status", stops.c.status,
            "purchase_amount", stops.c.purchase_amount,
            "receipt_photo_url", _public_url(stops.c.receipt_key),
            "completed_at", _iso_z(stops.c.completed_at),
        )
    )  # fmt: skip
    agg = func.jsonb_agg(aggregate_order_by(entry, stops.c.seq))
    return select(func.coalesce(agg, EMPTY_ARRAY)).where(stops.c.order_id == orders.c.id).scalar_subquery()


def _issues() -> Any:
    reporter = users.alias("issue_reporter")
    reporter_courier = couriers.alias("issue_courier")
    entry = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            "type", issues.c.issue_type,
            "description", issues.c.description,
            "photo_url", _public_url(issues.c.photo_key),
            "reported_at", _iso_z(issues.c.created_at),
            "reported_by", reporter.c.email,
            "courier_id", cast(reporter_courier.c.id, Text),
        )
    )  # fmt: skip
    agg = func.jsonb_agg(aggregate_order_by(entry, issues.c.created_at, issues.c.id))
    return (
        select(func.coalesce(agg, EMPTY_ARRAY))
        .select_from(
            issues.outerjoin(reporter, reporter.c.id == issues.c.reporter_id).outerjoin(
                reporter_courier, reporter_courier.c.user_id == issues.c.reporter_id
            )
        )
        .where(issues.c.order_id == orders.c.id)
        .scalar_subquery()
    )


def _stock_check_entry(c: Any) -> Any:
    """One stock check, keys of app.services.stock_checks.view (without the computed ones)."""
    return func.jsonb_build_object(
        "id", cast(c.c.id, Text),
        "status", c.c.status,
        "missing_text", c.c.missing_text,
        "substitute_text", c.c.substitute_text,
        "substitute_price", c.c.substitute_price,
        "photo_url", _public_url(c.c.photo_key),
        "nothing_available", c.c.nothing_available,
        "decided_by", c.c.decided_by,
        "created_at", _iso_z(c.c.created_at),
        "deadline_at", _iso_z(c.c.deadline_at),
        "decided_at", _iso_z(c.c.decided_at),
    )  # fmt: skip


def _latest_stock_check() -> Any:
    c = checks.alias("latest_stock_check")
    return (
        select(_stock_check_entry(c))
        .where(c.c.order_id == orders.c.id)
        .order_by(c.c.created_at.desc(), c.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _stock_checks() -> Any:
    c = checks.alias("all_stock_checks")
    agg = func.jsonb_agg(aggregate_order_by(_stock_check_entry(c), c.c.created_at, c.c.id))
    return select(func.coalesce(agg, EMPTY_ARRAY)).where(c.c.order_id == orders.c.id).scalar_subquery()


def _has_case() -> Any:
    return exists().where(cases.c.order_id == orders.c.id)


# The customer answered, or the courier reached him (triggerEmergencyContact set
# customer_responded_to_emergency for both).
ANSWERED = ("customer_confirmed", "courier_reached")


def _case_when_answered(column: Any) -> Any:
    return case((latest_case.c.resolution.in_(ANSWERED), column), else_=null())


def customer_while_assigned(user: CurrentUser) -> Any:
    """The order's customer while a courier is on his delivery, and admins (the courier's vehicle
    and plate: how to recognise him at the door)."""
    if user.is_admin:
        return true()
    return and_(
        orders.c.customer_id == user.id,
        orders.c.courier_id.is_not(None),
        orders.c.status.in_(ot.LIVE_STATUSES),
    )


def customer_or_admin(user: CurrentUser) -> Any:
    return true() if user.is_admin else orders.c.customer_id == user.id


def _preparing_offers() -> Any:
    """Couriers with the offer sheet open (intent refreshed in the last 3 min) and no pending
    offer yet (app/services/offer_intents.py)."""
    intents = OfferIntent.__table__
    offers = OrderOffer.__table__
    return (
        select(func.count())
        .where(
            intents.c.order_id == orders.c.id,
            intents.c.updated_at >= func.now() - INTENT_TTL,
            ~exists().where(
                offers.c.order_id == intents.c.order_id,
                offers.c.courier_id == intents.c.courier_id,
                offers.c.status == "pending",
            ),
        )
        .scalar_subquery()
    )


def _courier_ratings() -> Any:
    return (
        select(func.coalesce(courier_stats.c.ratings_count, 0))
        .where(courier_stats.c.courier_id == orders.c.courier_id)
        .scalar_subquery()
    )


delivered = orders.c.status == "delivered"
purchase_plus_fee = orders.c.purchase_amount + func.coalesce(orders.c.delivery_fee, 0)
guarded = {"read_guard": party_or_admin}

FIELDS: dict[str, LegacyField] = {
    "customer_id": LegacyField(customer.c.email, "string"),
    "customer_name": LegacyField(orders.c.contact_name, "string"),
    "customer_phone": LegacyField(orders.c.contact_phone_e164, "string", **guarded),
    "courier_id": LegacyField(orders.c.courier_id, "id"),
    "courier_user_id": LegacyField(courier_user.c.email, "string"),
    "courier_name": LegacyField(courier.c.display_name, "string"),
    "courier_phone": LegacyField(courier.c.phone_e164, "string"),
    "courier_photo": LegacyField(cast(null(), Text), "string"),
    "courier_live_lat": LegacyField(lat_of(live.c.location), "number", **guarded),
    "courier_live_lng": LegacyField(lng_of(live.c.location), "number", **guarded),
    "courier_live_at": LegacyField(live.c.recorded_at, "datetime", **guarded),
    "items_text": LegacyField(orders.c.items_text, "string"),
    "quantity": LegacyField(orders.c.quantity, "integer"),
    "notes": LegacyField(orders.c.notes, "string"),
    "alternatives": LegacyField(orders.c.alternatives, "string"),
    # "Si un article est indisponible" (NewOrder): call_me | substitute | skip | cancel.
    "unavailable_policy": LegacyField(orders.c.unavailable_policy, "string"),
    "estimated_price": LegacyField(orders.c.estimated_price, "number"),
    "package_size": LegacyField(cast(orders.c.package, Text), "string"),
    "shop_name": LegacyField(stop0.c.name, "string"),
    "shop_address": LegacyField(stop0.c.address, "string"),
    "shop_phone": LegacyField(stop0.c.phone, "string"),
    "shop_governorate": LegacyField(stop0.c.governorate, "string"),
    "shop_city": LegacyField(stop0.c.city, "string"),
    "shop_lat": LegacyField(lat_of(stop0.c.location), "number"),
    "shop_lng": LegacyField(lng_of(stop0.c.location), "number"),
    "shops": LegacyField(_shops(), "array"),
    "current_shop_index": LegacyField(orders.c.current_stop_seq, "integer"),
    "delivery_address": LegacyField(orders.c.delivery_address, "string"),
    "delivery_governorate": LegacyField(orders.c.delivery_governorate, "string"),
    "delivery_city": LegacyField(orders.c.delivery_city, "string"),
    "delivery_details": LegacyField(orders.c.delivery_details, "string", **guarded),
    "delivery_lat": LegacyField(lat_of(orders.c.delivery_location), "number"),
    "delivery_lng": LegacyField(lng_of(orders.c.delivery_location), "number"),
    "preferred_time": LegacyField(
        case((orders.c.scheduled_for.is_(None), literal("asap")), else_=literal("scheduled")), "string"
    ),
    "scheduled_time": LegacyField(orders.c.scheduled_for, "datetime"),
    "status": LegacyField(cast(orders.c.status, Text), "string"),
    "purchase_amount": LegacyField(orders.c.purchase_amount, "number"),
    "price_confirmed_by_customer": LegacyField(orders.c.price_confirmed_at.is_not(None), "boolean"),
    "delivery_fee": LegacyField(orders.c.delivery_fee, "number"),
    "total_amount": LegacyField(
        case((orders.c.purchase_amount.is_(None), null()), else_=purchase_plus_fee), "number"
    ),
    "payment_method": LegacyField(orders.c.payment_method, "string"),
    "payment_status": LegacyField(literal("pending"), "string"),
    "receipt_photo_url": LegacyField(
        select(_public_url(stops.c.receipt_key))
        .where(stops.c.order_id == orders.c.id, stops.c.seq == orders.c.current_stop_seq)
        .scalar_subquery(),
        "string",
    ),
    # Commission (src/lib/commission.js commissionFieldsAtDelivery), history only: nothing is deducted.
    "platform_fee": LegacyField(case((delivered, literal(0)), else_=null()), "number"),
    "courier_net_earning": LegacyField(
        case((delivered, func.coalesce(orders.c.delivery_fee, 0)), else_=null()), "number"
    ),
    "ods_commission": LegacyField(commission.c.amount, "number"),
    "ods_commission_status": LegacyField(
        case(
            *[(commission.c.kind == kind, literal(status)) for kind, status in LEGACY_STATUS.items()],
            else_=null(),
        ),
        "string",
    ),
    "cancelled_by": LegacyField(orders.c.cancelled_by, "string"),
    "cancellation_reason": LegacyField(orders.c.cancel_reason, "string"),
    "cancelled_at": LegacyField(orders.c.cancelled_at, "datetime"),
    "resale_order_id": LegacyField(orders.c.resale_deal_id, "id"),
    "distance_km": LegacyField(orders.c.distance_km, "number"),
    "eta_minutes": LegacyField(orders.c.eta_minutes, "integer"),
    "customer_rating": LegacyField(rating.c.rating, "integer"),
    "rating_comment": LegacyField(rating.c.comment, "string"),
    # "Client ne répond pas": the order's latest case (the procedure itself is another module).
    "no_response_reported": LegacyField(_has_case(), "boolean"),
    "no_response_reported_at": LegacyField(latest_case.c.started_at, "datetime"),
    "emergency_contact_started_at": LegacyField(latest_case.c.started_at, "datetime"),
    "emergency_contact_initiated": LegacyField(_has_case(), "boolean"),
    "customer_responded_to_emergency": LegacyField(
        func.coalesce(latest_case.c.resolution.in_(ANSWERED), False), "boolean"
    ),
    "customer_responded_at": LegacyField(_case_when_answered(latest_case.c.resolved_at), "datetime"),
    "no_response_deadline_at": LegacyField(latest_case.c.deadline_at, "datetime"),
    "no_response_final_at": LegacyField(latest_case.c.final_at, "datetime"),
    "no_response_resolution": LegacyField(latest_case.c.resolution, "string"),
    "no_response_case_id": LegacyField(latest_case.c.id, "id"),
    "no_response_channels": LegacyField(latest_case.c.channels, "object"),
    "reported_issues": LegacyField(_issues(), "array", **guarded),
    # "Article indisponible" (app/services/stock_checks.py): the latest check and all of them.
    "stock_check": LegacyField(_latest_stock_check(), "object", **guarded),
    "stock_checks": LegacyField(_stock_checks(), "array", **guarded),
    "has_issues": LegacyField(exists().where(issues.c.order_id == orders.c.id), "boolean", **guarded),
    "status_history": LegacyField(_history(), "array"),
    "preferred_courier_id": LegacyField(orders.c.preferred_courier_id, "id"),
    "courier_stats_recorded_at": LegacyField(orders.c.delivered_at, "datetime"),
    "last_dispatched_at": LegacyField(orders.c.last_dispatched_at, "datetime"),
    "accepted_at": LegacyField(orders.c.accepted_at, "datetime"),
    "delivered_at": LegacyField(orders.c.delivered_at, "datetime"),
    # Aurora: the courier on his way, for the customer (and admins).
    "courier_vehicle_model": LegacyField(
        courier.c.vehicle_model, "string", read_guard=customer_while_assigned
    ),
    "courier_plate": LegacyField(courier.c.vehicle_plate, "string", read_guard=customer_while_assigned),
    "courier_rating_count": LegacyField(_courier_ratings(), "integer", read_guard=customer_while_assigned),
    # "Un livreur prépare une offre…" (signalOfferIntent), for the customer and admins.
    "preparing_offers": LegacyField(_preparing_offers(), "integer", read_guard=customer_or_admin),
}
ORDER_FIELDS = set(FIELDS) | {"geocode_status"}


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    if await get_document(session, ENTITY, actor, doc_id) is None:
        raise ApiError(404, "not_found", "Order not found")
    order = await ot.lock_order(session, doc_id)
    if order is None:
        raise ApiError(404, "not_found", "Order not found")
    mine = (
        await session.execute(select(couriers.c.id).where(couriers.c.user_id == actor.id))
    ).scalar_one_or_none()
    if order.courier_id is not None and order.courier_id == mine and order.status in ot.LIVE_STATUSES:
        await order_steps.courier_step(session, actor, order, data, ORDER_FIELDS)
    elif order.customer_id == actor.id:
        await order_steps.customer_geocode(session, order, data, ORDER_FIELDS)
    else:
        raise order_steps.denied()


ENTITY = register(
    EntityDef(
        name="Order",
        source=orders.join(customer, customer.c.id == orders.c.customer_id)
        .outerjoin(courier, courier.c.id == orders.c.courier_id)
        .outerjoin(courier_user, courier_user.c.id == courier.c.user_id)
        .outerjoin(stop0, and_(stop0.c.order_id == orders.c.id, stop0.c.seq == 0))
        .outerjoin(live, live.c.order_id == orders.c.id)
        .outerjoin(rating, rating.c.order_id == orders.c.id)
        .outerjoin(latest_case, true())
        .outerjoin(commission, true()),
        id_expr=orders.c.id,
        id_type="uuid",
        created_expr=orders.c.created_at,
        updated_expr=orders.c.updated_at,
        created_by_expr=customer.c.email,
        fields=FIELDS,
        read_policy=read_policy,
        update=update,
    )
)
