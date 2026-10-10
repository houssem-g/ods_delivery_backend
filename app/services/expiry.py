"""Closing orders nobody moves any more (expireStaleOrders).

- pending / offers_received without activity for 24 h → cancelled by 'system', reason
  'expired_no_offer' (no offer ever) or 'expired'; its pending offers → expired; the
  customer is told once. Nobody is blamed.
- accepted … client_no_response without activity for 48 h → cancelled, reason
  'abandoned', courier and live position cleared, no penalty, no incident (an open
  no-response case is closed — resolution 'auto_closed', the table's word for it —
  and a legacy incident is withdrawn); both parties told once.
- pending offers whose order is closed → expired (hourly).
"Activity" = `couriers.last_activity_expr` (order writes, status events, no-response
steps; never the live position). Bounded per run (oldest first); each order is locked
and checked again before it is closed, so a customer acting at that second keeps it.
"""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, NoResponseCase, Order, OrderOffer
from app.realtime.events import emit
from app.services import order_texts
from app.services import order_transitions as ot
from app.services.couriers import last_activity_expr
from app.services.notifications import retire_acceptance_notices
from app.services.offers import close_pending_offers
from app.services.order_notices import notify_always_pushed
from app.services.orders import first_stop, mirror_incidents

log = logging.getLogger("odsd.expiry")
OPEN_EXPIRE = timedelta(hours=24)
RUNNING_EXPIRE = timedelta(hours=48)
MAX_CLOSURES = 10
MAX_OFFER_CLOSURES = 20
SCAN_LIMIT = 200


def _stale_condition(now: Any) -> Any:
    activity = last_activity_expr()
    return or_(
        and_(Order.status.in_(ot.OPEN_STATUSES), activity < now - OPEN_EXPIRE),
        and_(Order.status.in_(ot.LIVE_STATUSES), activity < now - RUNNING_EXPIRE),
    )


async def _expire_open(session: AsyncSession, order: Order) -> str:
    never_offered = order.status == "pending"
    reason = "expired_no_offer" if never_offered else "expired"
    order.cancelled_by, order.cancel_reason = "system", reason
    await ot.transition(session, order, "cancelled", None, "expireStaleOrders", reason, cancelled_by="system")
    await close_pending_offers(session, OrderOffer.order_id == order.id, "expired", MAX_OFFER_CLOSURES)
    stop = await first_stop(session, order.id)
    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_="order_cancelled",
        order_id=order.id,
        metadata={
            "reason": reason,
            "cancelled_by": "system",
            "recipient_role": "customer",
            "auto_expired": True,
        },
        **order_texts.expired_open(
            stop.name if stop else None, never_offered, int(OPEN_EXPIRE.total_seconds() // 3600)
        ),
    )
    return reason


async def _abandon(session: AsyncSession, order: Order) -> str:
    reason = "abandoned"
    courier = await session.get(Courier, order.courier_id) if order.courier_id else None
    was_no_response = order.status == "client_no_response"
    order.cancelled_by, order.cancel_reason = "system", reason
    await ot.transition(session, order, "cancelled", None, "expireStaleOrders", reason, cancelled_by="system")
    order.courier_id = None
    await session.flush()
    if courier is not None:  # R1: no « Offre acceptée » left for an order that ended
        await retire_acceptance_notices(session, order.id, courier.user_id)
    if was_no_response:
        cases = (
            await session.execute(
                select(NoResponseCase).where(NoResponseCase.order_id == order.id).with_for_update()
            )
        ).scalars()
        for case in cases:
            legacy_counted = case.messaging_status == "legacy" and case.incident_counted
            if case.status == "resolved" and not legacy_counted:
                continue
            if case.status != "resolved":
                case.status, case.resolution, case.resolved_at = "resolved", "auto_closed", ot.now_utc()
            if legacy_counted:
                case.incident_counted = False
            emit(session, "NoResponseCase", "update", case.id)
            if legacy_counted:
                await mirror_incidents(session, order.customer_id)
    stop = await first_stop(session, order.id)
    shop = stop.name if stop else None
    hours = int(RUNNING_EXPIRE.total_seconds() // 3600)
    meta = {"reason": reason, "cancelled_by": "system", "auto_closed": True}
    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_="order_cancelled",
        order_id=order.id,
        metadata={**meta, "recipient_role": "customer"},
        **order_texts.abandoned(shop, hours, for_courier=False),
    )
    if courier is not None and courier.user_id != order.customer_id:
        await notify_always_pushed(
            session,
            user_id=courier.user_id,
            type_="order_cancelled",
            order_id=order.id,
            metadata={**meta, "recipient_role": "courier"},
            **order_texts.abandoned(shop, hours, for_courier=True),
        )
    return reason


async def expire_stale_orders(session: AsyncSession) -> dict[str, Any]:
    now = ot.now_utc()
    activity = last_activity_expr()
    due = list(
        (
            await session.execute(
                select(Order.id, Order.status)
                .where(_stale_condition(now))
                .order_by(activity.asc(), Order.id)
                .limit(MAX_CLOSURES)
            )
        ).all()
    )
    closed: list[dict[str, str]] = []
    errors = 0
    for order_id, status in due:
        try:
            async with session.begin_nested():
                # Locked and checked again: the order may have moved since the list.
                order = (
                    await session.execute(
                        select(Order)
                        .where(Order.id == order_id, Order.status == status, _stale_condition(ot.now_utc()))
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if order is None:
                    continue
                if status in ot.OPEN_STATUSES:
                    reason = await _expire_open(session, order)
                else:
                    reason = await _abandon(session, order)
                closed.append({"id": str(order.id), "from": status, "reason": reason})
        except Exception:  # one bad order never blocks the others
            errors += 1
            log.exception("expire_stale_orders: closing %s failed", order_id)
    return {"success": True, "stale": len(due), "closed": closed, "errors": errors}


async def expire_orphan_offers(session: AsyncSession) -> int:
    """Pending offers left on an order that is no longer open → expired."""
    open_order = select(Order.id).where(Order.id == OrderOffer.order_id, Order.status.in_(ot.OPEN_STATUSES))
    ids = await close_pending_offers(session, not_(open_order.exists()), "expired", MAX_OFFER_CLOSURES)
    return len(ids)
