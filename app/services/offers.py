"""Courier offers: create (createOrderOffer), change the price (updateOrderOffer), rank
(getOfferRank), accept (acceptOrderOffer), withdraw (OrderOffer.delete).

Every write locks the order row first, so an offer, a withdrawal, an acceptance and a
cancellation of the same order run one after the other; the partial unique indexes
(`one_live_offer_per_courier`, `one_accepted_offer`) back the rules up.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Notification, OfferIntent, Order, OrderOffer, User, courier_stats
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services import step_notices, text_filter
from app.services.geo import as_float
from app.services.notifications import notify
from app.services.orders import SUSPENDED_AT, OrderRefused, active_incidents, courier_of_user, dropped_by
from app.services.safety import is_blocked

MAX_FEE_TND = Decimal(
    "200"
)  # order_offers / orders CHECK (the Deno function allowed 500; none above 200 exist)
MESSAGE_MAX = 300
MAX_EDITS = 10  # price changes of one offer (updateOrderOffer)
EDIT_PUSH_EVERY = timedelta(minutes=2)  # at most one push to the customer per offer in this window
RANK_BATCH_MAX = 20  # getOfferRank {order_ids}: the courier's "Mes offres" list in one call
OFFER_UPDATED = "offer_updated"  # Notification.data.kind of a price change (type new_offer)


def _parse_fee(value: Any) -> Decimal | None:
    number = as_float(value)
    if number is None:
        return None
    fee = Decimal(str(round(number, 3)))
    return fee if 0 < fee <= MAX_FEE_TND else None


def _parse_eta(value: Any) -> int | None:
    raw = as_float(value)
    return round(raw) if raw is not None and 1 <= round(raw) <= 600 else None


def _parse_message(value: Any) -> str | None:
    # the offer's message is shown to the customer: objectionable words masked (App Review 1.2)
    return text_filter.mask(value.strip()[:MESSAGE_MAX])[0] if isinstance(value, str) else None


async def verified_courier(session: AsyncSession, user: CurrentUser) -> Courier:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    if courier.verification in ("pending", "rejected"):
        raise OrderRefused(403, "courier_not_verified")
    return courier


async def offer_rank(
    session: AsyncSession, order_id: uuid.UUID, courier_id: uuid.UUID, fee: Decimal
) -> dict[str, Any]:
    """Where `fee` stands among the OTHER couriers' pending offers on the order. rank = 1 +
    strictly cheaper others; ties share the rank (`tied` others at the same price).
    `other_fees`: the other pending prices, ascending, anonymous (never an id or a name): the
    owner wants couriers to see the prices to beat (2026-09-30), `lowest_other` the cheapest."""
    others = (
        OrderOffer.order_id == order_id,
        OrderOffer.status == "pending",
        OrderOffer.courier_id != courier_id,
    )
    total, cheaper, tied = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(OrderOffer.proposed_fee < fee),
                func.count().filter(OrderOffer.proposed_fee == fee),
            ).where(*others)
        )
    ).one()
    fees = [
        float(f)
        for f in (
            await session.execute(
                select(OrderOffer.proposed_fee).where(*others).order_by(OrderOffer.proposed_fee)
            )
        ).scalars()
    ]
    return {
        "rank": 1 + cheaper,
        "total": total + 1,
        "cheapest": cheaper == 0,
        "tied": tied,
        "other_fees": fees,
        "lowest_other": fees[0] if fees else None,
    }


async def _pending_offer_of(
    session: AsyncSession, order_id: uuid.UUID, courier_id: uuid.UUID
) -> OrderOffer | None:
    return (
        await session.execute(
            select(OrderOffer).where(
                OrderOffer.order_id == order_id,
                OrderOffer.courier_id == courier_id,
                OrderOffer.status == "pending",
            )
        )
    ).scalar_one_or_none()


def _my_offer(offer: OrderOffer | None) -> dict[str, Any] | None:
    return {"id": str(offer.id), "fee": float(offer.proposed_fee)} if offer else None


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


async def get_rank(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """getOfferRank. One order: `fee` (the price he is typing) or his pending offer's price.
    Several (`order_ids`, his offers list): his pending offers only, orders without one or
    no longer open are left out."""
    courier = await verified_courier(session, user)
    raw_ids = payload.get("order_ids")
    if raw_ids is not None:
        if not isinstance(raw_ids, list) or len(raw_ids) > RANK_BATCH_MAX:
            raise OrderRefused(400, "invalid_order_ids", max=RANK_BATCH_MAX)
        wanted = {u for u in (_as_uuid(v) for v in raw_ids) if u is not None}
        mine = (
            await session.execute(
                select(OrderOffer)
                .join(Order, Order.id == OrderOffer.order_id)
                .where(
                    OrderOffer.order_id.in_(list(wanted)),
                    OrderOffer.courier_id == courier.id,
                    OrderOffer.status == "pending",
                    Order.status.in_(ot.OPEN_STATUSES),
                )
            )
        ).scalars()
        ranks = {}
        for offer in mine:
            rank = await offer_rank(session, offer.order_id, courier.id, offer.proposed_fee)
            ranks[str(offer.order_id)] = {**rank, "my_offer": _my_offer(offer)}
        return {"success": True, "ranks": ranks}

    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise OrderRefused(400, "Missing order_id")
    fee = None
    if payload.get("fee") is not None:
        fee = _parse_fee(payload.get("fee"))
        if fee is None:
            raise OrderRefused(400, "invalid_fee")
    order_uuid = _as_uuid(order_id)
    order = await session.get(Order, order_uuid) if order_uuid else None
    if order is None:
        raise OrderRefused(404, "order_not_found")
    if order.customer_id == user.id:
        raise OrderRefused(403, "own_order")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)
    mine_offer = await _pending_offer_of(session, order.id, courier.id)
    if fee is None:
        if mine_offer is None:
            raise OrderRefused(404, "offer_not_found")
        fee = mine_offer.proposed_fee
    rank = await offer_rank(session, order.id, courier.id, fee)
    return {"success": True, **rank, "my_offer": _my_offer(mine_offer)}


async def update_offer(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> tuple[OrderOffer, dict[str, Any]]:
    """updateOrderOffer: the courier changes the price (and the delay / note) of his pending
    offer on an open order. The customer hears it in-app (type new_offer, data.kind
    offer_updated); pushed at most once per offer every EDIT_PUSH_EVERY. MAX_EDITS per offer,
    counted on those notifications (the customer may delete his own: only he gets more then).
    An unchanged offer is answered as is."""
    raw_id = payload.get("offer_id")
    if not raw_id:
        raise OrderRefused(400, "Missing offer_id")
    fee = _parse_fee(payload.get("fee"))
    if fee is None:
        raise OrderRefused(400, "invalid_fee")
    courier = await verified_courier(session, user)
    offer_uuid = _as_uuid(raw_id)
    probe = (
        (
            await session.execute(select(OrderOffer.order_id).where(OrderOffer.id == offer_uuid))
        ).scalar_one_or_none()
        if offer_uuid
        else None
    )
    if probe is None:
        raise OrderRefused(404, "offer_not_found")
    order = await ot.lock_order(session, probe)
    offer = (
        await session.execute(select(OrderOffer).where(OrderOffer.id == offer_uuid).with_for_update())
    ).scalar_one()
    if order is None or offer.courier_id != courier.id or offer.status == "withdrawn":
        raise OrderRefused(404, "offer_not_found")  # someone else's offer: as if it did not exist
    if offer.status != "pending":
        raise OrderRefused(409, "offer_not_pending")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)

    eta = _parse_eta(payload.get("eta_minutes")) if "eta_minutes" in payload else None
    message = _parse_message(payload.get("message")) if "message" in payload else None
    new_eta = eta if eta is not None else offer.eta_minutes
    new_message = (message or None) if message is not None else offer.message
    previous_fee = offer.proposed_fee
    if fee == previous_fee and new_eta == offer.eta_minutes and new_message == offer.message:
        return offer, await offer_rank(session, order.id, courier.id, fee)

    updates = Notification.__table__.c
    edits = select(func.count()).where(
        updates.order_id == order.id,
        updates.type == "new_offer",
        updates.data["kind"].astext == OFFER_UPDATED,
        updates.data["offer_id"].astext == str(offer.id),
    )
    if (await session.execute(edits)).scalar_one() >= MAX_EDITS:
        raise OrderRefused(429, "too_many_edits", max=MAX_EDITS)
    pushed_lately = (
        await session.execute(edits.where(updates.created_at >= ot.now_utc() - EDIT_PUSH_EVERY))
    ).scalar_one() > 0

    offer.proposed_fee, offer.eta_minutes, offer.message = fee, new_eta, new_message
    await session.flush()
    emit(session, "OrderOffer", "update", offer.id)
    name = courier.display_name or "Le livreur"
    name_ar = courier.display_name or "المندوب"
    delay_fr = f" · ~{new_eta} min" if new_eta else ""
    delay_ar = f" · ~{new_eta} د" if new_eta else ""
    await notify(
        session,
        user_id=order.customer_id,
        type_="new_offer",
        title_fr="Offre modifiée",
        title_ar="تم تعديل العرض",
        body_fr=f"{name} a modifié son offre : {fee:.3f} TND{delay_fr}",
        body_ar=f"{name_ar} عدّل عرضه: {fee:.3f} د.ت{delay_ar}",
        order_id=order.id,
        metadata={
            "kind": OFFER_UPDATED,
            "offer_id": str(offer.id),
            "courier_id": str(courier.id),
            "courier_name": courier.display_name,
            "proposed_fee": float(fee),
            "previous_fee": float(previous_fee),
            "eta_minutes": new_eta,
            "recipient_role": "customer",
        },
        push=not pushed_lately,
    )
    return offer, await offer_rank(session, order.id, courier.id, fee)


async def create_offer(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> OrderOffer:
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise OrderRefused(400, "Missing order_id")
    fee = _parse_fee(payload.get("fee"))
    if fee is None:
        raise OrderRefused(400, "invalid_fee")
    eta = _parse_eta(payload.get("eta_minutes"))
    dist_raw = as_float(payload.get("distance_km"))
    distance = Decimal(str(round(dist_raw, 2))) if dist_raw is not None and 0 <= dist_raw <= 1000 else None
    message = _parse_message(payload.get("message")) or ""

    courier = await verified_courier(session, user)
    order = await ot.lock_order(session, order_id)
    if order is None:
        raise OrderRefused(404, "order_not_found")
    if order.customer_id == user.id:
        raise OrderRefused(403, "own_order")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)
    if user.id in await dropped_by(session, order.id):
        raise OrderRefused(409, "order_dropped")  # he cancelled this delivery himself
    if await is_blocked(session, user.id, order.customer_id):
        raise OrderRefused(409, "blocked")
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
    intent = await session.execute(  # his "prépare une offre" is now an offer
        delete(OfferIntent)
        .where(OfferIntent.order_id == order.id, OfferIntent.courier_id == courier.id)
        .returning(OfferIntent.order_id)
    )
    if intent.first() is not None:
        emit(session, "Order", "update", order.id)  # preparing_offers changes
    if order.status == "pending":
        await ot.transition(session, order, "offers_received", user, "createOrderOffer")
    await step_notices.offer_created(session, order, offer, courier)
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
    if courier is not None and await is_blocked(session, courier.user_id, order.customer_id):
        raise OrderRefused(409, "blocked")
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
    await step_notices.offer_accepted(session, order, selected, courier_user.id)
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
