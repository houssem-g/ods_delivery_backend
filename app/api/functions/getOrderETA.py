"""getOrderETA — time for the courier to reach the shop (before the purchase) or the customer:
OSRM route when reachable (timeout), straight-line estimate at
the vehicle's speed otherwise. The order's parties and admins only.

Body: { order_id }. Returns { success, order_id, status, eta_minutes, distance_km, source | reason }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Order
from app.security.deps import CurrentUser
from app.services.orders import is_assigned_courier
from app.services.tracking import eta_destination, order_eta


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id = payload.get("order_id")
    if not order_id:
        return 400, {"error": "Missing required field: order_id"}
    oid = as_uuid(order_id)
    order = await session.get(Order, oid) if oid else None
    if order is None:
        return 404, {"error": "Order not found"}
    if not (
        user.is_admin or order.customer_id == user.id or await is_assigned_courier(session, order, user.id)
    ):
        return 403, {"error": "Forbidden"}
    destination = await eta_destination(session, order)
    answer = await order_eta(session, order, destination)
    answer["order_id"] = order_id
    return 200, answer
