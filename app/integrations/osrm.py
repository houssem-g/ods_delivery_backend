"""OSRM route (duration, distance) for getOrderETA. Off when OSRM_URL is empty; any failure,
timeout or empty answer returns None and the caller uses its straight-line estimate."""

import logging

import httpx

from app.config import settings

log = logging.getLogger("odsd.osrm")

# Tests replace it with an httpx.MockTransport (nothing leaves the machine).
transport: httpx.AsyncBaseTransport | None = None


async def route(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> tuple[float, float] | None:
    """(duration seconds, distance metres) of the driving route, or None."""
    base = settings.OSRM_URL.rstrip("/")
    if not base:
        return None
    url = f"{base}/route/v1/driving/{from_lng},{from_lat};{to_lng},{to_lat}"
    try:
        async with httpx.AsyncClient(timeout=settings.OSRM_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(url, params={"overview": "false"})
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("OSRM unavailable: %s", type(exc).__name__)
        return None
    routes = data.get("routes") if isinstance(data, dict) else None
    if not routes or not isinstance(routes[0], dict):
        return None
    try:
        return float(routes[0].get("duration") or 0), float(routes[0].get("distance") or 0)
    except (TypeError, ValueError):
        return None
