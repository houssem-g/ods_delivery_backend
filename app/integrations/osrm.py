"""OSRM route (duration, distance) for getOrderETA. Off when OSRM_URL is empty; any failure,
timeout or empty answer returns None and the caller uses its straight-line estimate."""

import logging
from dataclasses import dataclass, field

import httpx

from app.config import settings

log = logging.getLogger("odsd.osrm")

# Tests replace it with an httpx.MockTransport (nothing leaves the machine).
transport: httpx.AsyncBaseTransport | None = None


MAX_POINTS = 2000


@dataclass(frozen=True)
class Route:
    duration_s: float
    distance_m: float
    coords: list[list[float]] = field(default_factory=list)  # [[lat, lng], ...]


def _coords(geometry: object) -> list[list[float]]:
    points = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(points, list):
        return []
    out = []
    for point in points[:MAX_POINTS]:
        if (
            isinstance(point, list)
            and len(point) >= 2
            and all(isinstance(v, (int, float)) for v in point[:2])
        ):
            out.append([float(point[1]), float(point[0])])
    return out


async def route(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> Route | None:
    """Duration, distance and the road geometry of the driving route, or None."""
    base = settings.OSRM_URL.rstrip("/")
    if not base:
        return None
    url = f"{base}/route/v1/driving/{from_lng},{from_lat};{to_lng},{to_lat}"
    try:
        async with httpx.AsyncClient(timeout=settings.OSRM_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(url, params={"overview": "full", "geometries": "geojson"})
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("OSRM unavailable: %s", type(exc).__name__)
        return None
    routes = data.get("routes") if isinstance(data, dict) else None
    if not routes or not isinstance(routes[0], dict):
        return None
    try:
        return Route(
            duration_s=float(routes[0].get("duration") or 0),
            distance_m=float(routes[0].get("distance") or 0),
            coords=_coords(routes[0].get("geometry")),
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Leg:
    duration_s: float
    distance_m: float


@dataclass(frozen=True)
class Trip:
    legs: list[Leg]
    coords: list[list[float]]  # the whole path, [[lat, lng], ...]
    leg_coords: list[list[list[float]]]  # the path of each leg


async def trip(points: list[tuple[float, float]]) -> Trip | None:
    """Driving route through `points` ([(lat, lng), ...], 2 or more): one leg per stretch, with
    its own road geometry (the courier's map draws « vous → magasin » and « livraison » apart).
    None when OSRM is off, slow or answers nothing usable."""
    base = settings.OSRM_URL.rstrip("/")
    if not base or len(points) < 2:
        return None
    where = ";".join(f"{lng},{lat}" for lat, lng in points)
    url = f"{base}/route/v1/driving/{where}"
    try:
        async with httpx.AsyncClient(timeout=settings.OSRM_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(
                url, params={"overview": "full", "geometries": "geojson", "steps": "true"}
            )
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("OSRM unavailable: %s", type(exc).__name__)
        return None
    routes = data.get("routes") if isinstance(data, dict) else None
    if not routes or not isinstance(routes[0], dict):
        return None
    raw_legs = routes[0].get("legs")
    if not isinstance(raw_legs, list) or len(raw_legs) != len(points) - 1:
        return None
    try:
        legs = [Leg(float(leg.get("duration") or 0), float(leg.get("distance") or 0)) for leg in raw_legs]
        leg_coords = []
        for leg in raw_legs:
            path: list[list[float]] = []
            for step in leg.get("steps") or []:
                for point in _coords(step.get("geometry") if isinstance(step, dict) else None):
                    if not path or path[-1] != point:
                        path.append(point)
            leg_coords.append(path[:MAX_POINTS])
    except (AttributeError, TypeError, ValueError):
        return None
    return Trip(legs=legs, coords=_coords(routes[0].get("geometry")), leg_coords=leg_coords)
