"""The single path for order status changes.

Every change of `orders.status` goes through `transition()` (and the creation through
`start()`): it checks the matrix below, sets the status and its timestamp
(accepted_at, delivered_at, cancelled_at), appends the `order_status_events` row the
legacy `status_history` is built from, takes the courier's live position off an
order that leaves the delivery, writes the commission entry at 'delivered' and
queues the realtime `Order` update. It does NOT notify anybody: each caller sends
its own notifications (texts differ per flow) with `app.services.notifications.notify`.

The caller holds the order row lock (`lock_order`) and owns the transaction. The
matrix is the union of every legitimate flow; callers restrict it further for
their actor (e.g. `order_steps.COURIER_STEPS` for the courier's own writes).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Notification, OfferIntent, Order, OrderOffer, OrderStatusEvent, OrderTracking
from app.realtime.events import emit
from app.services import commission, partner_webhooks

log = logging.getLogger("odsd.order_transitions")

OPEN_STATUSES = ("pending", "offers_received")
# Statuses of a delivery in progress (trackCourierLocation LIVE_STATUSES, getOrderCourier tracking).
LIVE_STATUSES = (
    "accepted", "at_shop", "price_confirmation_needed", "purchased", "on_the_way", "client_no_response",
)  # fmt: skip
TERMINAL_STATUSES = ("delivered", "collected", "cancelled")
# « Je viens chercher » (app/services/vitrine_pickup.py): the restaurant's steps, never a courier's
PICKUP_STATUSES = ("awaiting_restaurant", "preparing", "ready", "collected")

_BACK_TO_POOL_OR_CANCEL = frozenset({"pending", "cancelled"})

ALLOWED: dict[str | None, frozenset[str]] = {
    # creation: placeOrder ('pending'); a hot-deal reservation creates an order already 'accepted'
    # (« Je viens chercher »: 'awaiting_restaurant', app/services/vitrine_pickup.py)
    None: frozenset({"pending", "accepted", "awaiting_restaurant"}),
    # first offer (createOrderOffer); last offer withdrawn; offer accepted; cancelled / expired
    "pending": frozenset({"offers_received", "accepted", "cancelled"}),
    "offers_received": frozenset({"pending", "accepted", "cancelled"}),
    # courier steps; 'pending' = the courier drops the delivery (back to the pool)
    # price_confirmation_needed from accepted: a Vitrine restaurant reports a missing dish before the
    # courier arrives (app/services/vitrine_orders.py); the order goes back to accepted afterwards.
    "accepted": frozenset({"at_shop", "on_the_way", "price_confirmation_needed"}) | _BACK_TO_POOL_OR_CANCEL,
    "at_shop": frozenset({"price_confirmation_needed", "purchased"}) | _BACK_TO_POOL_OR_CANCEL,
    "price_confirmation_needed": frozenset({"at_shop", "purchased", "accepted"}) | _BACK_TO_POOL_OR_CANCEL,
    # client_no_response: triggerEmergencyContact reports once the goods are bought (REPORTABLE)
    "purchased": frozenset({"on_the_way", "client_no_response"}) | _BACK_TO_POOL_OR_CANCEL,
    "on_the_way": frozenset({"delivered", "client_no_response"}) | _BACK_TO_POOL_OR_CANCEL,
    # no-response procedure: courier resumes / reaches the customer / closes
    "client_no_response": frozenset({"on_the_way", "delivered"}) | _BACK_TO_POOL_OR_CANCEL,
    "delivered": frozenset(),
    "cancelled": frozenset(),
    # « Je viens chercher »: placeOrder creates it 'awaiting_restaurant'; ODS reports the kitchen's steps
    # (a step may be skipped: the customer pays while it is still being prepared); the restaurant refuses
    # before preparing, the customer cancels before that too; « Pas venu » closes a ready one.
    "awaiting_restaurant": frozenset({"preparing", "ready", "collected", "cancelled"}),
    "preparing": frozenset({"ready", "collected", "cancelled"}),
    "ready": frozenset({"collected", "cancelled"}),
    "collected": frozenset(),
}
COURIER_REQUIRED = frozenset(LIVE_STATUSES) | {"delivered"}


class InvalidTransition(Exception):
    def __init__(self, from_status: str | None, to_status: str) -> None:
        super().__init__(f"order status {from_status} -> {to_status} is not allowed")
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: str | None, to_status: str) -> bool:
    return to_status in ALLOWED.get(from_status, frozenset())


def _actor_id(actor: Any) -> uuid.UUID | None:
    if actor is None or isinstance(actor, uuid.UUID):
        return actor
    return actor.id


def now_utc() -> datetime:
    return datetime.now(UTC)


async def lock_order(session: AsyncSession, order_id: uuid.UUID | str | None) -> Order | None:
    """The order row, locked for this transaction (None if the id is not an order)."""
    try:
        oid = order_id if isinstance(order_id, uuid.UUID) else uuid.UUID(str(order_id))
    except (TypeError, ValueError):
        return None
    return (
        await session.execute(
            select(Order).where(Order.id == oid).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def clear_live_position(session: AsyncSession, order_id: uuid.UUID) -> None:
    """The courier's live position leaves the order (legacy CLEAR_LIVE_POSITION)."""
    await session.execute(delete(OrderTracking).where(OrderTracking.order_id == order_id))


def _event(
    order: Order,
    from_status: str | None,
    to_status: str,
    actor: Any,
    source: str,
    reason: str | None,
    cancelled_by: str | None,
    location: tuple[float, float] | None,
    at: datetime,
) -> OrderStatusEvent:
    return OrderStatusEvent(
        order_id=order.id,
        from_status=from_status,
        to_status=to_status,
        actor_user_id=_actor_id(actor),
        source=source,
        reason=(reason or None) and reason[:500],
        cancelled_by=cancelled_by,
        location=f"SRID=4326;POINT({location[1]} {location[0]})" if location else None,
        created_at=at,
    )


async def start(
    session: AsyncSession, order: Order, actor: Any, source: str, status: str = "pending"
) -> None:
    """Creation of a new (already added) order: its first status event."""
    if not can_transition(None, status):
        raise InvalidTransition(None, status)
    if (getattr(order, "fulfillment", "delivery") == "pickup") != (status == "awaiting_restaurant"):
        raise InvalidTransition(None, status)
    at = now_utc()
    order.status = status
    if status == "accepted":
        order.accepted_at = order.accepted_at or at
    await session.flush()
    session.add(_event(order, None, status, actor, source, None, None, None, at))
    await session.flush()
    emit(session, "Order", "create", order.id)


PUNCTUALITY_BOUND = 24 * 60  # minutes kept either side (a course forgotten open for days is noise)


def punctuality_late_minutes(
    accepted_at: datetime | None, eta_minutes: int | None, delivered_at: datetime
) -> int | None:
    """Delivered minus announced (accepted_at + the delay of his offer), whole minutes; negative =
    early. None without an acceptance time or a delay (a resold hot deal has no delay)."""
    if accepted_at is None or eta_minutes is None:
        return None
    late = round((delivered_at - accepted_at).total_seconds() / 60 - eta_minutes)
    return max(-PUNCTUALITY_BOUND, min(PUNCTUALITY_BOUND, late))


ORDER_CLOSED = "order_closed"  # the Order signal's data.kind (QA 19/10 M11)


async def announce_closed(session: AsyncSession, order: Order, to_status: str) -> None:
    """An open order was taken by a courier or cancelled (QA 19/10 M11): the couriers who saw it can no
    longer read the row, so its `update` never reached them (the card stayed ~48 s, the « Nouvelle
    commande » alert up to 75 s). They get an Order `signal` instead — the order id only, no personal data:
    {kind: "order_closed", id, status: "closed", reason: "taken" | "cancelled"}. Who saw it: the couriers
    told of it (new_order notifications), those who offered or were preparing an offer; never the customer
    nor the courier who took it (he reads the row)."""
    told = select(Notification.user_id).where(
        Notification.order_id == order.id, Notification.type == "new_order"
    )
    offered = (
        select(Courier.user_id)
        .join(OrderOffer, OrderOffer.courier_id == Courier.id)
        .where(OrderOffer.order_id == order.id)
    )
    preparing = (
        select(Courier.user_id)
        .join(OfferIntent, OfferIntent.courier_id == Courier.id)
        .where(OfferIntent.order_id == order.id)
    )
    users = set((await session.execute(union(told, offered, preparing))).scalars())
    users.discard(order.customer_id)
    if order.courier_id is not None:
        taker = (
            await session.execute(select(Courier.user_id).where(Courier.id == order.courier_id))
        ).scalar_one_or_none()
        users.discard(taker)
    if not users:
        return
    emit(
        session,
        "Order",
        "signal",
        order.id,
        audience=users,
        data={
            "kind": ORDER_CLOSED,
            "id": str(order.id),
            "status": "closed",
            "reason": "cancelled" if to_status == "cancelled" else "taken",
        },
    )


async def transition(
    session: AsyncSession,
    order: Order,
    to_status: str,
    actor: Any,
    source: str,
    reason: str | None = None,
    *,
    cancelled_by: str | None = None,
    location: tuple[float, float] | None = None,
) -> OrderStatusEvent:
    """Moves `order` (locked by the caller) to `to_status`. Raises InvalidTransition.

    actor: the user causing it (CurrentUser / User / uuid) or None for the system.
    source: the flow's name, kept in the event (legacy status_history[].source).
    cancelled_by / reason: kept on the event (and by the cancel flows on the order).
    location: (lat, lng) of the actor when known (the courier's app sends it).
    """
    from_status = order.status
    if not can_transition(from_status, to_status):
        raise InvalidTransition(from_status, to_status)
    pickup = getattr(order, "fulfillment", "delivery") == "pickup"
    if to_status != "cancelled" and pickup != (to_status in PICKUP_STATUSES):
        raise InvalidTransition(from_status, to_status)  # a courier step on a pickup order, or the reverse
    if to_status in COURIER_REQUIRED and order.courier_id is None:
        raise InvalidTransition(from_status, to_status)
    at = now_utc()
    order.status = to_status
    if to_status == "accepted":
        if from_status != "price_confirmation_needed":  # not a return after a stock check
            order.accepted_at = at
    elif to_status == "delivered":
        order.delivered_at = at
        order.punctuality_late_minutes = punctuality_late_minutes(order.accepted_at, order.eta_minutes, at)
    elif to_status == "cancelled":
        order.cancelled_at = at
    elif to_status == "preparing":
        order.preparing_at = at
    elif to_status == "ready":
        order.ready_at = at
    elif to_status == "collected":
        order.collected_at = at
    if to_status not in LIVE_STATUSES:
        await clear_live_position(session, order.id)
    event = _event(order, from_status, to_status, actor, source, reason, cancelled_by, location, at)
    session.add(event)
    await session.flush()
    if to_status == "delivered":
        await commission.record_delivery(session, order, at)
    emit(session, "Order", "update", order.id)
    if from_status in OPEN_STATUSES and to_status not in OPEN_STATUSES:
        await announce_closed(session, order, to_status)
    if order.partner_id is not None:
        # A restaurant ODS order (Lot 2): the partner hears about it (queued, sent by the job).
        try:
            async with session.begin_nested():
                await partner_webhooks.on_transition(
                    session, order, from_status, to_status, reason, cancelled_by
                )
        except Exception:  # a webhook never fails the delivery step itself
            log.exception("partner webhook not queued: order %s %s -> %s", order.id, from_status, to_status)
    elif order.source == "vitrine":
        # A Vitrine order of a shop linked to ODS: the kitchen hears of it. Its CREATION in ODS is
        # synchronous and must fail the step when ODS does not take it (vitrine_sync.ConfirmFailed); the later
        # events are queued and never fail the step.
        from app.services import vitrine_orders, vitrine_sync

        try:
            async with session.begin_nested():
                await vitrine_orders.on_transition(
                    session, order, from_status, to_status, reason, cancelled_by
                )
        except vitrine_sync.ConfirmFailed:
            raise
        except Exception:
            log.exception("vitrine webhook not queued: order %s %s -> %s", order.id, from_status, to_status)
    return event
