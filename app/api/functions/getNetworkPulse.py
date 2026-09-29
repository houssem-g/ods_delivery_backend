"""getNetworkPulse — live supply around a point, for the Welcome screen (anonymous) and the
customer's home and order form (app/services/pulse.py).

Body: { lat?, lng?, radius_km? (default 3, max 10), shop_lat?, shop_lng? } (points outside the
service country are ignored).
Returns { success, city, online_city, online_near (null without a point), radius_km,
first_offer_minutes (median minutes to the first offer, orders of the last 14 days within 15 km;
null under 5 samples) }; signed in, also { courier_dots: [{lat, lng}] (≤ 30, 0.004° grid cells,
never a real position) } and, with a shop, { couriers_near_shop (online within 1 km),
fee_range: {min, max} | null (the nearby online couriers' tariffs for shop → point, 0.5 steps) }.
Errors: too_many_pulse_requests (429; anonymous: RATE_LIMIT_PULSE_ANONYMOUS per IP, signed in:
RATE_LIMIT_PULSE per user).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.rate_limit import allow, ip_key
from app.security.deps import CurrentUser
from app.services import pulse

AUTH = "optional"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if user is None:
        key, rate = ip_key(request), settings.RATE_LIMIT_PULSE_ANONYMOUS
    else:
        key, rate = f"user:{user.id}", settings.RATE_LIMIT_PULSE
    if not allow("getNetworkPulse", key, rate):
        return 429, {"error": "too_many_pulse_requests", "limit": rate}
    return 200, await pulse.network_pulse(session, payload, signed_in=user is not None)
