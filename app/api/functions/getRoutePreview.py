"""getRoutePreview — the road route of an order the courier is looking at (owner, 10/10/2026: the
Commandes map drew straight lines and the card estimated « 5 min per km »): courier → shop →
customer by the roads (OSRM), each stretch with its distance, duration and path.

Body: { order_id, from_lat?, from_lng? } — `from` is the position the app uses for the courier
(his GPS, or his address while abroad); outside Tunisia it is ignored (shop → customer only).
Readable orders only (the Order entity's read rules: an open order a verified courier may bid on,
his own deliveries, admins). Answers are kept SHORT_TTL in memory (positions rounded to ~100 m),
so a courier moving the list around does not call OSRM each time.

Times are the courier's vehicle's (leg_minutes), not a car's.

Returns { success: true, source: "osrm", vehicle, to_shop: {distance_km, duration_min, path} | null,
delivery: {distance_km, duration_min, path}, distance_km, drive_minutes, eta_minutes (drive +
SHOPPING_MINUTES) } or { success: false, reason: "unavailable" | "no_location" } (the app keeps
its straight-line estimate). Errors: order_id missing (400), order not found or not readable (404).
"""

import time
from collections import OrderedDict
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.compat.entities.order import orders, read_policy
from app.integrations import osrm
from app.models import OrderStop
from app.security.deps import CurrentUser
from app.services.geo import ORDER_BOUNDS, as_float, lat_of, lng_of, within
from app.services.orders import courier_of_user
from app.services.tracking import speed_kmh

SHOPPING_MINUTES = 15  # same as src/lib/orderFlow.js offerEtaMinutes
SHORT_TTL = 15 * 60
MAX_CACHED = 1000
_cache: OrderedDict[tuple, tuple[float, osrm.Trip]] = OrderedDict()


def _key(points: list[tuple[float, float]]) -> tuple:
    return tuple((round(lat, 3), round(lng, 3)) for lat, lng in points)


async def _trip(points: list[tuple[float, float]]) -> osrm.Trip | None:
    key, now = _key(points), time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < SHORT_TTL:
        _cache.move_to_end(key)
        return hit[1]
    found = await osrm.trip(points)
    if found is not None:
        _cache[key] = (now, found)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)
    return found


def leg_minutes(leg: osrm.Leg, vehicle: str | None) -> float:
    """The courier's time on this stretch: OSRM times a car with free roads, so the road km at the
    vehicle's average city speed (scooter 25 km/h, car 30, walking 5 — the live tracking's speeds)
    wins when it is slower (owner, 10/10/2026: « estimation pour livreur en scooter »)."""
    return max(leg.duration_s / 60, leg.distance_m / 1000 / speed_kmh(vehicle) * 60)


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
    lat, lng = as_float(payload.get("from_lat")), as_float(payload.get("from_lng"))
    with_courier = within(lat, lng, ORDER_BOUNDS)
    if with_courier:
        points.insert(0, (float(lat), float(lng)))  # type: ignore[arg-type]
    found = await _trip(points)
    if found is None:
        return 200, {"success": False, "reason": "unavailable"}
    courier = await courier_of_user(session, user.id)
    vehicle = courier.vehicle if courier is not None else None
    legs = [_leg(leg, path, vehicle) for leg, path in zip(found.legs, found.leg_coords, strict=True)]
    drive = sum(leg_minutes(leg, vehicle) for leg in found.legs)
    return 200, {
        "success": True,
        "source": "osrm",
        "vehicle": vehicle or "scooter",
        "to_shop": legs[0] if with_courier else None,
        "delivery": legs[-1],
        "distance_km": round(sum(leg.distance_m for leg in found.legs) / 1000, 2),
        "drive_minutes": round(drive),
        "eta_minutes": round(drive) + SHOPPING_MINUTES,
    }
