"""getOfferEstimate — the courier's arrival at the customer for an order he is pricing (owner,
18/10/2026, the offer sheet's « Votre temps jusqu'au client »): courier → shop (his position, his
vehicle) + the shop's average wait (the same for every courier) + shop → customer
(app/services/arrival.py: the one computation the offers store and the map's card shows).

Body: { order_id, from_lat?, from_lng? } — `from`: the position the app uses for him (else the
server's recent one; abroad: none). Readable orders only (the Order entity's read rules, like
getRoutePreview) and a courier profile.

Returns { success: true, order_id, vehicle, source: "osrm" | "straight_line",
to_shop: {distance_km, minutes} | null (position unknown), wait: {minutes, source: "measured" |
"default", samples, shops}, delivery: {distance_km, minutes}, total_minutes | null,
announce_margin_minutes } or { success: false, reason: "no_location" }.
Errors: order_id missing (400), courier_profile_missing (403), order not found / not readable (404),
too_many_estimate_requests (429, RATE_LIMIT_OFFER_ESTIMATE per courier).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.compat.entities.order import orders, read_policy
from app.config import settings
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import arrival
from app.services.order_transitions import now_utc
from app.services.orders import courier_of_user


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not payload.get("order_id"):
        return 400, {"error": "Missing required field: order_id"}
    if not allow("getOfferEstimate", f"user:{user.id}", settings.RATE_LIMIT_OFFER_ESTIMATE):
        return 429, {"error": "too_many_estimate_requests", "limit": settings.RATE_LIMIT_OFFER_ESTIMATE}
    courier = await courier_of_user(session, user.id)
    if courier is None:
        return 403, {"error": "courier_profile_missing"}
    oid = as_uuid(str(payload.get("order_id")))
    readable = (
        (await session.execute(select(orders.c.id).where(orders.c.id == oid, read_policy(user)))).first()
        if oid
        else None
    )
    if readable is None:
        return 404, {"error": "Order not found"}
    origin = await arrival.courier_origin(session, courier, payload, now_utc())
    found = await arrival.arrival(session, oid, courier.vehicle, origin)  # type: ignore[arg-type]
    if found is None:
        return 200, {"success": False, "reason": "no_location"}
    return 200, {"success": True, "order_id": str(oid), **found.as_dict()}
