"""getCustomerReliability — a customer's "client ne répond pas" record of the last 180 days and
what it implies (base44/functions/getCustomerReliability, src/lib/noResponsePolicy.js).

Body: { order_id? } — without: the caller's own record; with: the order's customer, for the
customer, the assigned courier, any verified courier while the order is open, admins. Couriers
see the level from 2 incidents on. Returns { success, incidents, level, visible_to_couriers,
max_advance_tnd, phone_confirmation_required, suspended, window_days }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Order
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.orders import active_incidents, courier_of_user, reliability_from_count


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    customer_id = user.id
    raw_id = str(payload.get("order_id") or "")
    if raw_id:
        oid = as_uuid(raw_id)
        order = await session.get(Order, oid) if oid else None
        if order is None:
            return 404, {"error": "Order not found"}
        allowed = user.is_admin or order.customer_id == user.id
        if not allowed:
            courier = await courier_of_user(session, user.id)
            allowed = courier is not None and (
                order.courier_id == courier.id
                or (order.status in ot.OPEN_STATUSES and courier.verification == "verified")
            )
        if not allowed:
            return 403, {"error": "Forbidden"}
        customer_id = order.customer_id
    result = reliability_from_count(await active_incidents(session, customer_id))
    if customer_id != user.id and not user.is_admin and not result["visible_to_couriers"]:
        return 200, {"success": True, **reliability_from_count(0)}
    return 200, {"success": True, **result}
