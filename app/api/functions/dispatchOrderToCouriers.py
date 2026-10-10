"""dispatchOrderToCouriers — the customer (or an admin) broadcasts his open order again
(our own flows call app.services.dispatch directly).

Body: { order_id }. At most once every 5 minutes (429 too_soon, retry_after_s); only an open
order (409 order_not_open). Returns { success, order_id, dispatched, skipped, candidates_total,
preferred_notified, sample_skips } (or { success, dispatched: 0, reason: 'test_order' }).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.dispatch import dispatch_order, redispatch_wait_seconds

REDISPATCH_MIN_SECONDS = 5 * 60


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id = payload.get("order_id")
    if not order_id:
        return 400, {"error": "Missing order_id"}
    order = await ot.lock_order(session, order_id)
    if order is None:
        return 404, {"error": "Order not found"}
    if not user.is_admin and order.customer_id != user.id:
        return 403, {"error": "Forbidden"}
    if order.status not in ot.OPEN_STATUSES:
        return 409, {"error": "order_not_open", "status": order.status}
    wait = redispatch_wait_seconds(order, ot.now_utc(), REDISPATCH_MIN_SECONDS)
    if wait > 0:
        return 429, {"error": "too_soon", "retry_after_s": wait}
    return 200, await dispatch_order(session, order)
