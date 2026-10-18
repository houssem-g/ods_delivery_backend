"""getRoutePreview — the road route of an order the courier is looking at (owner, 10/10/2026: the
Commandes map drew straight lines and the card estimated « 5 min per km »): courier → shop →
customer by the roads (OSRM), each stretch with its distance, duration and path.

Body: { order_id, from_lat?, from_lng? } — `from` is the position the app uses for the courier
(his GPS, or his address while abroad); outside Tunisia it is ignored (shop → customer only), and
without it the server's recent position of the courier is used (arrival.courier_origin).
Readable orders only (the Order entity's read rules: an open order a verified courier may bid on,
his own deliveries, admins). Answers are kept SHORT_TTL in memory (positions rounded to ~100 m),
so a courier moving the list around does not call OSRM each time.

Times are the courier's vehicle's (leg_minutes), not a car's. `eta_minutes` is the arrival at the
customer computed by app/services/arrival.py (courier → shop + the shop's average wait + shop →
customer, the same number as the offer sheet), `arrival` its three lines.

Returns { success: true, source: "osrm", vehicle, to_shop: {distance_km, duration_min, path} | null,
delivery: {distance_km, duration_min, path}, distance_km, drive_minutes, shop_wait_minutes,
eta_minutes (drive + the shop's wait), arrival } or { success: false, reason: "unavailable" |
"no_location" } (the app keeps its straight-line estimate). Errors: order_id missing (400), order
not found or not readable (404).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.compat.entities.order import orders, read_policy
from app.integrations import osrm
from app.models import OrderStop
from app.security.deps import CurrentUser
from app.services import arrival as arrival_service
from app.services.arrival import leg_minutes, road_trip
from app.services.geo import lat_of, lng_of
from app.services.order_transitions import now_utc
from app.services.orders import courier_of_user

# The cache lives in app/services/arrival.py (the offer sheet's estimate reuses the same trips).
_cache = arrival_service.trip_cache
_trip = road_trip


def _leg(leg: osrm.Leg, path: list[list[float]], vehicle: str | None) -> dict[str, Any]:
    return {
        "distance_km": round(leg.distance_m / 1000, 2),
        "duration_min": round(leg_minutes(leg, vehicle), 1),
        "path": [[round(lat, 5), round(lng, 5)] for lat, lng in path],
    }


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    oid = as_uuid(str(payload.get("order_id") or ""))
    if not payload.get("order_id"):
        return 400, {"error": "Missing required field: order_id"}
    row = (
        (
            await session.execute(
                select(lat_of(orders.c.delivery_location), lng_of(orders.c.delivery_location)).where(
                    orders.c.id == oid, read_policy(user)
                )
            )
        ).first()
        if oid
        else None
    )
    if row is None:
        return 404, {"error": "Order not found"}
    shop = (
        await session.execute(
            select(lat_of(OrderStop.location), lng_of(OrderStop.location)).where(
                OrderStop.order_id == oid, OrderStop.seq == 0
            )
        )
    ).first()
    if shop is None or shop[0] is None or row[0] is None:
        return 200, {"success": False, "reason": "no_location"}
    points = [(float(shop[0]), float(shop[1])), (float(row[0]), float(row[1]))]
    courier = await courier_of_user(session, user.id)
    vehicle = courier.vehicle if courier is not None else None
    origin = await arrival_service.courier_origin(session, courier, payload, now_utc())
    with_courier = origin is not None
    if with_courier:
        points.insert(0, origin)  # type: ignore[arg-type]
    found = await _trip(points)
    if found is None:
        return 200, {"success": False, "reason": "unavailable"}
    legs = [_leg(leg, path, vehicle) for leg, path in zip(found.legs, found.leg_coords, strict=True)]
    drive = sum(leg_minutes(leg, vehicle) for leg in found.legs)
    # the same arrival as the offer sheet and the offers (one computation): drive + the shop's wait
    eta = await arrival_service.arrival(session, oid, vehicle, origin)  # type: ignore[arg-type]
    wait = eta.wait_min if eta is not None else arrival_service.DEFAULT_WAIT_MINUTES
    total = eta.total_min if eta is not None and eta.total_min is not None else round(drive) + wait
    return 200, {
        "success": True,
        "source": "osrm",
        "vehicle": vehicle or "scooter",
        "to_shop": legs[0] if with_courier else None,
        "delivery": legs[-1],
        "distance_km": round(sum(leg.distance_m for leg in found.legs) / 1000, 2),
        "drive_minutes": round(drive),
        "shop_wait_minutes": wait,
        "eta_minutes": total,
        "arrival": eta.as_dict() if eta is not None else None,
    }
