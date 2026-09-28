"""Courier offers: create (createOrderOffer), accept (acceptOrderOffer), withdraw (OrderOffer.delete).

Every write locks the order row first, so an offer, a withdrawal, an acceptance and a
cancellation of the same order run one after the other; the partial unique indexes
(`one_live_offer_per_courier`, `one_accepted_offer`) back the rules up.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Order, OrderOffer, User, courier_stats
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.geo import as_float
from app.services.orders import SUSPENDED_AT, OrderRefused, active_incidents, courier_of_user

MAX_FEE_TND = Decimal(
    "200"
)  # order_offers / orders CHECK (the Deno function allowed 500; none above 200 exist)
MESSAGE_MAX = 300


def _parse_fee(value: Any) -> Decimal | None:
    number = as_float(value)
    if number is None:
        return None
    fee = Decimal(str(round(number, 3)))
    return fee if 0 < fee <= MAX_FEE_TND else None


async def create_offer(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> OrderOffer:
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise OrderRefused(400, "Missing order_id")
    fee = _parse_fee(payload.get("fee"))
    if fee is None:
        raise OrderRefused(400, "invalid_fee")
    eta_raw = as_float(payload.get("eta_minutes"))
    eta = round(eta_raw) if eta_raw is not None and 1 <= round(eta_raw) <= 600 else None
    dist_raw = as_float(payload.get("distance_km"))
    distance = Decimal(str(round(dist_raw, 2))) if dist_raw is not None and 0 <= dist_raw <= 1000 else None
    message = payload.get("message").strip()[:MESSAGE_MAX] if isinstance(payload.get("message"), str) else ""

    courier = await courier_of_user(session, user.id)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    if courier.verification in ("pending", "rejected"):
        raise OrderRefused(403, "courier_not_verified")
    order = await ot.lock_order(session, order_id)
    if order is None:
        raise OrderRefused(404, "order_not_found")
    if order.customer_id == user.id:
        raise OrderRefused(403, "own_order")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)
    already = (
        await session.execute(
            select(OrderOffer.id).where(
                OrderOffer.order_id == order.id,
                OrderOffer.courier_id == courier.id,
                OrderOffer.status == "pending",
            )
        )
    ).first()
    if already is not None:
        raise OrderRefused(409, "offer_already_sent")
    rating = (
        await session.execute(
            select(func.coalesce(courier_stats.c.average_rating, 5)).where(
                courier_stats.c.courier_id == courier.id
            )
        )
    ).scalar_one_or_none()
    offer = OrderOffer(
        order_id=order.id,
        courier_id=courier.id,
        proposed_fee=fee,
        eta_minutes=eta,
        distance_km=distance,
        message=message or None,
        courier_rating_snapshot=rating,
        status="pending",
    )
    try:
        async with session.begin_nested():
            session.add(offer)
            await session.flush()
    except IntegrityError as exc:  # a double tap that passed the check at the same time
        raise OrderRefused(409, "offer_already_sent") from exc
    emit(session, "OrderOffer", "create", offer.id)
    if order.status == "pending":
        await ot.transition(session, order, "offers_received", user, "createOrderOffer")
    return offer


async def accept_offer(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> tuple[Order, OrderOffer, User]:
    order_id, offer_id = payload.get("order_id"), payload.get("offer_id")
    if not order_id or not offer_id:
        raise OrderRefused(400, "Missing order_id or offer_id")
    order = await ot.lock_order(session, str(order_id))
    if order is None:
        raise OrderRefused(404, "order_not_found")
    if order.customer_id != user.id and not user.is_admin:
        raise OrderRefused(403, "Forbidden")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)
    if not user.is_admin:
        incidents = await active_incidents(session, order.customer_id)
        if incidents >= SUSPENDED_AT:
            raise OrderRefused(403, "customer_suspended", incidents=incidents)
    try:
        offer_uuid = uuid.UUID(str(offer_id))
    except ValueError:
        raise OrderRefused(404, "offer_not_found") from None
    offers = list(
        (
            await session.execute(select(OrderOffer).where(OrderOffer.order_id == order.id).with_for_update())
        ).scalars()
    )
    selected = next((o for o in offers if o.id == offer_uuid), None)
    if selected is None or selected.status == "withdrawn":
        raise OrderRefused(404, "offer_not_found")
    if selected.status != "pending" or not selected.proposed_fee or selected.proposed_fee <= 0:
        raise OrderRefused(409, "offer_not_pending")
    courier = await session.get(Courier, selected.courier_id)
    courier_user = await session.get(User, courier.user_id) if courier else None
    if (
        courier is None
        or courier_user is None
        or courier_user.deleted_at is not None
        or courier.verification != "verified"
    ):
        raise OrderRefused(409, "courier_unavailable")

    now = ot.now_utc()
    selected.status, selected.decided_at = "accepted", now
    emit(session, "OrderOffer", "update", selected.id)
    for other in offers:
        if other.id != selected.id and other.status == "pending":
            other.status, other.decided_at = "rejected", now
            emit(session, "OrderOffer", "update", other.id)
    order.courier_id = courier.id
    order.delivery_fee = selected.proposed_fee
    order.distance_km = selected.distance_km if selected.distance_km is not None else order.distance_km
    order.eta_minutes = selected.eta_minutes
    await ot.clear_live_position(session, order.id)  # a new courier: never a former one's position
    await session.flush()
    await ot.transition(session, order, "accepted", user, "acceptOrderOffer")
    return order, selected, courier_user


async def withdraw_offer(session: AsyncSession, user: CurrentUser, offer_id: str) -> list[uuid.UUID]:
    """The courier takes back his pending offer (OrderOffer.delete). The row stays as 'withdrawn'
    (hidden from the entity); the order goes back to 'pending' when no pending offer is left.
    Returns the users who hear about the deletion."""
    try:
        oid = uuid.UUID(str(offer_id))
    except ValueError:
        raise OrderRefused(404, "not_found") from None
    probe = (
        await session.execute(select(OrderOffer.order_id).where(OrderOffer.id == oid))
    ).scalar_one_or_none()
    if probe is None:
        raise OrderRefused(404, "not_found")
    order = await ot.lock_order(session, probe)
    offer = (
        await session.execute(select(OrderOffer).where(OrderOffer.id == oid).with_for_update())
    ).scalar_one()
    courier = await session.get(Courier, offer.courier_id)
    is_owner = courier is not None and courier.user_id == user.id
    if (
        offer.status == "withdrawn"
        or order is None
        or not (is_owner or user.is_admin or order.customer_id == user.id)
    ):
        raise OrderRefused(404, "not_found")
    if not (is_owner or user.is_admin) or offer.status != "pending":
        raise OrderRefused(
            403, "permission_denied", message="Permission denied for delete operation on OrderOffer"
        )
    offer.status, offer.decided_at = "withdrawn", ot.now_utc()
    await session.flush()
    await demote_if_no_pending_offer(session, order.id, user, "offer_withdrawn")
    return [uid for uid in (courier.user_id if courier else None, order.customer_id) if uid is not None]


async def demote_if_no_pending_offer(
    session: AsyncSession, order_id: uuid.UUID, actor: Any, source: str
) -> bool:
    """An order 'offers_received' whose last pending offer went away is 'pending' again (the
    couriers' lists and the customer's home show it as waiting). Locks the order; True if moved."""
    order = await ot.lock_order(session, order_id)
    if order is None or order.status != "offers_received":
        return False
    left = (
        await session.execute(
            select(func.count())
            .select_from(OrderOffer)
            .where(OrderOffer.order_id == order.id, OrderOffer.status == "pending")
        )
    ).scalar_one()
    if left:
        return False
    await ot.transition(session, order, "pending", actor, source)
    return True


async def close_pending_offers(
    session: AsyncSession, where: Any, status: str = "expired", limit: int | None = None
) -> list[uuid.UUID]:
    """Pending offers matching `where` → `status` (expired / rejected). Returns their ids; events queued."""
    stmt = select(OrderOffer.id).where(OrderOffer.status == "pending", where).order_by(OrderOffer.created_at)
    if limit:
        stmt = stmt.limit(limit)
    ids = list((await session.execute(stmt)).scalars())
    if ids:
        await session.execute(
            update(OrderOffer).where(OrderOffer.id.in_(ids)).values(status=status, decided_at=ot.now_utc())
        )
        for offer_id in ids:
            emit(session, "OrderOffer", "update", offer_id)
    return ids
