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

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatusEvent, OrderTracking
from app.realtime.events import emit
from app.services import commission

OPEN_STATUSES = ("pending", "offers_received")
# Statuses of a delivery in progress (trackCourierLocation LIVE_STATUSES, getOrderCourier tracking).
LIVE_STATUSES = (
    "accepted", "at_shop", "price_confirmation_needed", "purchased", "on_the_way", "client_no_response",
)  # fmt: skip
TERMINAL_STATUSES = ("delivered", "cancelled")

_BACK_TO_POOL_OR_CANCEL = frozenset({"pending", "cancelled"})

ALLOWED: dict[str | None, frozenset[str]] = {
    # creation: placeOrder ('pending'); a hot-deal reservation creates an order already 'accepted'
    None: frozenset({"pending", "accepted"}),
    # first offer (createOrderOffer); last offer withdrawn; offer accepted; cancelled / expired
    "pending": frozenset({"offers_received", "accepted", "cancelled"}),
    "offers_received": frozenset({"pending", "accepted", "cancelled"}),
    # courier steps; 'pending' = the courier drops the delivery (back to the pool)
    "accepted": frozenset({"at_shop", "on_the_way"}) | _BACK_TO_POOL_OR_CANCEL,
    "at_shop": frozenset({"price_confirmation_needed", "purchased"}) | _BACK_TO_POOL_OR_CANCEL,
    "price_confirmation_needed": frozenset({"at_shop", "purchased"}) | _BACK_TO_POOL_OR_CANCEL,
    "purchased": frozenset({"on_the_way"}) | _BACK_TO_POOL_OR_CANCEL,
    "on_the_way": frozenset({"delivered", "client_no_response"}) | _BACK_TO_POOL_OR_CANCEL,
    # no-response procedure: courier resumes / reaches the customer / closes
    "client_no_response": frozenset({"on_the_way", "delivered"}) | _BACK_TO_POOL_OR_CANCEL,
    "delivered": frozenset(),
    "cancelled": frozenset(),
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
    at = now_utc()
    order.status = status
    if status == "accepted":
        order.accepted_at = order.accepted_at or at
    await session.flush()
    session.add(_event(order, None, status, actor, source, None, None, None, at))
    await session.flush()
    emit(session, "Order", "create", order.id)


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
    if to_status in COURIER_REQUIRED and order.courier_id is None:
        raise InvalidTransition(from_status, to_status)
    at = now_utc()
    order.status = to_status
    if to_status == "accepted":
        order.accepted_at = at
    elif to_status == "delivered":
        order.delivered_at = at
    elif to_status == "cancelled":
        order.cancelled_at = at
    if to_status not in LIVE_STATUSES:
        await clear_live_position(session, order.id)
    event = _event(order, from_status, to_status, actor, source, reason, cancelled_by, location, at)
    session.add(event)
    await session.flush()
    if to_status == "delivered":
        await commission.record_delivery(session, order, at)
    emit(session, "Order", "update", order.id)
    return event
