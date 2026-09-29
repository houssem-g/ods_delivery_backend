"""getDemandPulse — where and when the orders come from around a courier
(app/services/pulse.py).

Body: { lat, lng, radius_km? (default: his notification_radius_km; 1-50) }.
Returns { success, radius_km, zones: [{name, lat, lng, count, trend_pct}] (top 3 delivery areas —
orders.delivery_city, else the shop's city — orders of the last 2 h plus the open ones; centroid
of their delivery points; trend vs the same 2 h a week earlier, null when that was 0),
hourly: [{hour, avg}] (the next 6 hours, Africa/Tunis: orders created in that hour on the same
weekday, average of the last 4 weeks), peak: {from, to} | null (best 2-hour window of those),
orders_per_day (average of the last 14 days) }.
Errors: invalid_location (400), courier_profile_missing (403), too_many_pulse_requests (429,
RATE_LIMIT_DEMAND_PULSE per courier).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import pulse
from app.services.geo import as_float
from app.services.orders import courier_of_user


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        return 403, {"error": "courier_profile_missing"}
    if not allow("getDemandPulse", f"user:{user.id}", settings.RATE_LIMIT_DEMAND_PULSE):
        return 429, {"error": "too_many_pulse_requests", "limit": settings.RATE_LIMIT_DEMAND_PULSE}
    where = pulse.point_of(payload)
    if where is None:
        return 400, {"error": "invalid_location"}
    return 200, await pulse.demand_pulse(session, courier, where, as_float(payload.get("radius_km")))
