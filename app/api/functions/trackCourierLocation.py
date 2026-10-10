"""trackCourierLocation — the courier's GPS fix.

Written onto his orders in progress (order_tracking, read as Order.courier_live_* by the order's
parties, realtime Order update) except orders idle for 48 h (expire_stale_orders' activity rule),
and onto his profile (couriers.last_location / last_seen_at, the presence heartbeat).
There is no speed check.

Body: { courier_id, lat, lng }. Returns { success, message, validation_status, lat, lng,
updated_at, live_orders }. Errors: missing fields / invalid coordinates (400), Unauthorized (403).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.couriers import publish_position, valid_coordinates
from app.services.orders import courier_of_user


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    courier_id, lat, lng = payload.get("courier_id"), payload.get("lat"), payload.get("lng")
    if not courier_id or not isinstance(courier_id, str) or lat is None or lng is None:
        return 400, {"error": "Missing required fields: courier_id, lat, lng"}
    problem = valid_coordinates(lat, lng)
    if problem:
        return 400, {
            "error": "Invalid coordinates",
            "reason": problem,
            "validation_status": "geo_validation_failed",
        }
    courier = await courier_of_user(session, user.id, lock=True)
    if courier is None or str(courier.id) != courier_id.strip():
        return 403, {"error": "Unauthorized"}
    live = await publish_position(session, courier, float(lat), float(lng))
    return 200, {
        "success": True,
        "message": "Location updated",
        "validation_status": "all_checks_passed",
        "lat": lat,
        "lng": lng,
        "updated_at": legacy_datetime(ot.now_utc()),
        "live_orders": len(live),
    }
