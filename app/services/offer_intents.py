"""Offer intents: "un livreur prépare une offre…" on the customer's offers page.

A verified courier with the offer sheet of an open order open signals it (signalOfferIntent
{active: true}, refreshed while the sheet stays open); closing the sheet ({active: false}) or
sending the offer (createOrderOffer) deletes it. The Order field `preparing_offers` counts the
intents refreshed in the last INTENT_TTL by couriers without a pending offer on the order (an
offer is shown as such). Rows older than PURGE_AFTER are deleted by the hourly
`expire_orphan_offers` job. Counts only: the customer never learns who.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OfferIntent, Order
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.offers import verified_courier
from app.services.orders import OrderRefused, dropped_by

INTENT_TTL = timedelta(minutes=3)
PURGE_AFTER = timedelta(minutes=10)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


async def signal(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    raw_id, active = payload.get("order_id"), payload.get("active")
    if not raw_id:
        raise OrderRefused(400, "Missing order_id")
    if not isinstance(active, bool):
        raise OrderRefused(400, "invalid_active")
    courier = await verified_courier(session, user)
    order_id = _uuid(raw_id)
    order = await session.get(Order, order_id) if order_id else None
    if order is None:
        raise OrderRefused(404, "order_not_found")
    now = ot.now_utc()
    key = (OfferIntent.order_id == order.id, OfferIntent.courier_id == courier.id)
    previous = (await session.execute(select(OfferIntent.updated_at).where(*key))).scalar_one_or_none()
    fresh_before = previous is not None and previous >= now - INTENT_TTL
    if not active:
        if previous is not None:
            await session.execute(delete(OfferIntent).where(*key))
        if fresh_before:
            emit(session, "Order", "update", order.id)
        return {"success": True, "active": False}

    if order.customer_id == user.id:
        raise OrderRefused(403, "own_order")
    if order.status not in ot.OPEN_STATUSES:
        raise OrderRefused(409, "order_not_open", status=order.status)
    if user.id in await dropped_by(session, order.id):
        raise OrderRefused(409, "order_dropped")
    stmt = insert(OfferIntent).values(order_id=order.id, courier_id=courier.id, updated_at=now)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["order_id", "courier_id"], set_={"updated_at": now})
    )
    if not fresh_before:  # the count the customer sees changes
        emit(session, "Order", "update", order.id)
    return {"success": True, "active": True, "ttl_seconds": int(INTENT_TTL.total_seconds())}


async def purge_stale(session: AsyncSession) -> int:
    rows = await session.execute(
        delete(OfferIntent)
        .where(OfferIntent.updated_at < ot.now_utc() - PURGE_AFTER)
        .returning(OfferIntent.order_id)
    )
    return len(rows.all())
