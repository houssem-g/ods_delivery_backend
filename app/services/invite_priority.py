"""The invite-link head start (owner 18/10/2026): « Les clients inscrits par votre lien vous voient en
premier, et vous recevez leurs commandes avant tout le monde ».

When a customer attributed to a courier at sign-up (invite link, orders.signup_attribution) places an
order and that courier can take it (verified, free, not dropped / blocked, wants new-order pushes,
online or launch mode: dispatch._preferred), HE ALONE is told and may offer during HEAD_START (60 s):
the other couriers do not see the order at all (lists, map, notifications, getOfferRank, the chat,
« prépare une offre », createOrderOffer → 404 order_not_found, as for an order that does not exist).
The 60 s run even once he has offered (his lead is the point). Then the normal broadcast goes out.

- Once per order: a re-broadcast (customer's « relancer », a courier giving it back) never gives a
  new head start.
- Durable: orders.priority_until / priority_released_at; the job `invite_priority_release`
  (app/jobs/orders.py, every 5 s on the scheduler leader) sends the broadcast of the head starts that
  ended — after a restart too; under the order lock and released_at: idempotent. The order is
  visible to everyone from priority_until on, whatever the job's delay (Order read rule).
- QA orders: the head start only goes to a QA courier (dispatch._preferred qa_only), the broadcast
  after it to the QA couriers only. Vitrine delivery orders: same rule (the customer is the end
  client). Restaurant ODS orders (« Commande restaurant »): no attribution, never a head start.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Order, User
from app.realtime.events import emit
from app.services import order_transitions as ot
from app.services.orders import signup_attribution

HEAD_START = timedelta(seconds=60)
RELEASE_BATCH = 200


def running(order: Order, now: datetime) -> bool:
    """The head start is on (the other couriers do not see the order yet)."""
    return (
        order.priority_until is not None and order.priority_released_at is None and now < order.priority_until
    )


def hidden_from(order: Order, courier_id: uuid.UUID | None, now: datetime) -> bool:
    """True for every courier but the invite-link one while the head start runs."""
    return running(order, now) and courier_id != order.priority_courier_id


def hidden_sql(orders: Any, user_id: Any) -> Any:
    """SQL twin of hidden_from for the Order read rule (orders = the orders table, user_id = the reader)."""
    couriers = Courier.__table__.alias("priority_courier")
    return and_(
        orders.c.priority_until.is_not(None),
        orders.c.priority_released_at.is_(None),
        orders.c.priority_until > func.now(),
        not_(exists().where(couriers.c.id == orders.c.priority_courier_id, couriers.c.user_id == user_id)),
    )


def start(session: AsyncSession, order: Order, courier_id: uuid.UUID, now: datetime) -> datetime:
    order.priority_courier_id = courier_id
    order.priority_until = now + HEAD_START
    order.priority_released_at = None
    emit(session, "Order", "update", order.id)  # the customer's « Votre livreur … a 60 s »
    return order.priority_until


async def due_order_ids(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.execute(
        select(Order.id)
        .where(
            Order.priority_until.is_not(None),
            Order.priority_released_at.is_(None),
            Order.priority_until <= func.now(),
        )
        .order_by(Order.priority_until)
        .limit(RELEASE_BATCH)
    )
    return list(rows.scalars())


async def release(session: AsyncSession, order_id: uuid.UUID) -> dict[str, Any] | None:
    """Ends a head start that is due: the broadcast to the other couriers (open orders only).
    None when there is nothing (left) to do — another run did it, or it is not due yet."""
    from app.services.dispatch import dispatch_order

    order = await ot.lock_order(session, order_id)
    if order is None or order.priority_until is None or order.priority_released_at is not None:
        return None
    now = ot.now_utc()
    if now < order.priority_until:
        return None
    order.priority_released_at = now
    emit(session, "Order", "update", order.id)  # every courier's list may show it now
    if order.status not in ot.OPEN_STATUSES:
        return {"order_id": str(order.id), "dispatched": 0, "reason": order.status}
    return await dispatch_order(session, order, after_head_start=True)


async def invite_courier_id(session: AsyncSession, customer_id: uuid.UUID) -> uuid.UUID | None:
    """The customer's invite-link courier (sign-up attribution), his own courier account excluded."""
    customer = await session.get(User, customer_id)
    if customer is None:
        return None
    courier_id = signup_attribution(customer)
    if courier_id is None:
        return None
    own = (
        await session.execute(select(Courier.user_id).where(Courier.id == courier_id))
    ).scalar_one_or_none()
    return None if own is None or own == customer_id else courier_id
