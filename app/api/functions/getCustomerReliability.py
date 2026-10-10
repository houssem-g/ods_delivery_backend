"""getCustomerReliability — a customer's "client ne répond pas" record of the last 180 days and what it
implies (same rules as src/lib/noResponsePolicy.js).

Body: { order_id? } — without: the caller's own record; with: the order's customer, for the
customer, the assigned courier, any verified courier while the order is open, admins. Couriers see
the record from the first incident on. Returns { success, incidents, level, visible_to_couriers,
max_advance_tnd, phone_confirmation_required, suspended (always false: nobody is suspended since
10/10/2026), window_days, delivered_orders (orders delivered to this customer, all time),
recent_orders / recent_unpaid (his last 10 finished orders and how many he left unpaid: the
couriers' badge « n'a pas payé X fois sur ses Y dernières commandes »), reliability_pct (0-100:
delivered / (delivered + incidents of the window), 100 without any), avg_reply_seconds (median
seconds from a courier's message to the customer's next one, last 90 days; null under 3 samples),
rules (the incident thresholds every screen words its texts with: visible_to_couriers_at,
warning_at, limited_at, limited_max_advance_tnd, window_days), suspended_until (always null, kept
for older app versions) }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Order
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.orders import (
    active_incidents,
    courier_of_user,
    reliability_extras,
    reliability_from_count,
)


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
    incidents = await active_incidents(session, customer_id)
    result = reliability_from_count(incidents)
    extras = await reliability_extras(session, customer_id, incidents)
    # An unpaid order among the last ones shows the badge even once its incident left the window.
    result["visible_to_couriers"] = result["visible_to_couriers"] or extras["recent_unpaid"] > 0
    if customer_id != user.id and not user.is_admin and not result["visible_to_couriers"]:
        return 200, {"success": True, **reliability_from_count(0), **extras, "suspended_until": None}
    return 200, {"success": True, **result, **extras, "suspended_until": None}
