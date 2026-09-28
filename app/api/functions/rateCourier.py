"""rateCourier — the customer rates his delivered order (base44/functions/rateCourier).

One order_ratings row per order (a new rating replaces it); the courier's average is the
courier_stats view over all his ratings. The live position leaves the order.
Body: { order_id, rating (1-5), comment? }. Returns { success, average_rating }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import OrderRating, courier_stats
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.geo import as_float


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    raw = as_float(payload.get("rating"))
    rating = int(raw + 0.5) if raw is not None else None  # Math.round
    if not payload.get("order_id") or rating is None or not 1 <= rating <= 5:
        return 400, {"error": "order_id and a rating from 1 to 5 are required"}
    order = await ot.lock_order(session, as_uuid(payload.get("order_id")))
    if order is None:
        return 404, {"error": "Order not found"}
    if order.customer_id != user.id:
        return 403, {"error": "Only the customer of this order can rate it"}
    if order.status != "delivered":
        return 409, {"error": "order_not_delivered"}
    comment = payload.get("comment")[:1000] if isinstance(payload.get("comment"), str) else ""
    await ot.clear_live_position(session, order.id)
    average = None
    if order.courier_id is not None:
        values = {"courier_id": order.courier_id, "rater_id": user.id, "rating": rating, "comment": comment}
        await session.execute(
            insert(OrderRating)
            .values(order_id=order.id, **values)
            .on_conflict_do_update(index_elements=[OrderRating.order_id], set_=values)
        )
        found = (
            await session.execute(
                select(courier_stats.c.average_rating).where(courier_stats.c.courier_id == order.courier_id)
            )
        ).scalar_one_or_none()
        average = round(float(found), 2) if found is not None else None
        emit(session, "CourierProfile", "update", order.courier_id)
    emit(session, "Order", "update", order.id)
    return 200, {"success": True, "average_rating": average}
