"""HTTP clients of the OpenStreetMap services: Nominatim (geocoding) and Overpass (import).

Both send the configured User-Agent (their usage policies require an identifiable
application), use short timeouts and never raise on the caller's path:
`nominatim_search` answers None when the service is slow, down or busy, and the
geocoder falls back to the places index.

Tests replace the transport with `set_transport()`; nothing here is ever called for
real from the test suite.
"""

import asyncio
import logging
import math
import time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("odsd.osm")

_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Routes every OSM call through `transport` (tests); None restores the network."""
    global _transport
    _transport = transport


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport,
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": settings.OSM_USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )


class OverpassError(Exception):
    pass


# --- Nominatim ---------------------------------------------------------------------


class _Throttle:
    """At most one Nominatim call per NOMINATIM_MIN_INTERVAL_SECONDS in this process.

    A caller that would wait longer than NOMINATIM_MAX_WAIT_SECONDS gets False and
    skips Nominatim: a map screen geocoding 30 places at once must not queue for 30 s.
    """

    def __init__(self) -> None:
        self._next_slot = 0.0

    def reset(self) -> None:
        self._next_slot = 0.0

    def reserve(self) -> float | None:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        wait = slot - now
        if wait > settings.NOMINATIM_MAX_WAIT_SECONDS:
            return None
        self._next_slot = slot + settings.NOMINATIM_MIN_INTERVAL_SECONDS
        return wait


nominatim_throttle = _Throttle()


async def nominatim_search(query: str) -> list[dict[str, Any]] | None:
    """Nominatim /search restricted to Tunisia. [] = no result; None = unavailable/busy."""
    if not settings.NOMINATIM_ENABLED:
        return None
    wait = nominatim_throttle.reserve()
    if wait is None:
        log.info("nominatim busy: falling back to the places index")
        return None
    if wait > 0:
        await asyncio.sleep(wait)
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": "1",
        "countrycodes": "tn",
        "addressdetails": "1",
        "accept-language": "fr",
    }
    try:
        async with _client(settings.NOMINATIM_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{settings.NOMINATIM_URL.rstrip('/')}/search", params=params)
    except httpx.HTTPError as exc:
        log.warning("nominatim unavailable: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("nominatim answered %s", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            lat, lng = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lng):
            results.append({**item, "lat": lat, "lon": lng})
    return results


# --- Overpass -----------------------------------------------------------------------


async def pause(seconds: float) -> None:
    """A polite pause between Overpass calls (scaled by OVERPASS_DELAY_SCALE)."""
    delay = seconds * settings.OVERPASS_DELAY_SCALE
    if delay > 0:
        await asyncio.sleep(delay)


async def overpass_query(query: str) -> dict[str, Any]:
    """Runs an Overpass QL query on the first endpoint that answers (Deno `overpassFetch`)."""
    last_error: Exception | None = None
    for endpoint in settings.OVERPASS_URLS:
        try:
            async with _client(settings.OVERPASS_TIMEOUT_SECONDS) as client:
                response = await client.post(endpoint, data={"data": query})
        except httpx.HTTPError as exc:
            last_error = OverpassError(f"{type(exc).__name__} from {endpoint}")
            await pause(2.5)
            continue
        if response.status_code != 200:
            last_error = OverpassError(f"HTTP {response.status_code} from {endpoint}")
            await pause(1.5)
            continue
        try:
            data = response.json()
        except ValueError:
            last_error = OverpassError(f"invalid JSON from {endpoint}")
            continue
        if isinstance(data, dict):
            return data
        last_error = OverpassError(f"unexpected answer from {endpoint}")
    raise last_error or OverpassError("no Overpass endpoint configured")
