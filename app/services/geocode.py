"""geocodeAddress: an address → coordinates, for the map pickers and the order screens.

Order: `geocode_cache` → Nominatim (when enabled and not busy) → the places index
(the whole Deno implementation). Nominatim answers — found or not — are cached
(GEOCODE_CACHE_DAYS / GEOCODE_MISS_CACHE_HOURS) as its usage policy asks; the cache
is written in its own transaction so a "not found" answer (404, rolled back by the
function router) is remembered too. A Nominatim outage or a busy throttle is not
cached: the next call tries again.
"""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import transaction
from app.integrations import osm
from app.models import GeocodeCache
from app.security.tokens import now_utc
from app.services import text_norm
from app.services.places import geocode_from_places, js_round

log = logging.getLogger("odsd.geocode")

MAX_QUERY_CHARS = 300
MAX_TOKENS = 20


def query_tokens(address: Any, city: Any) -> list[str]:
    """Deno: tokens of `[address, city].join(' ').slice(0, 300)`, at most 20."""
    joined = " ".join(str(v) for v in (address, city) if v)[:MAX_QUERY_CHARS]
    return text_norm.tokenize(joined)[:MAX_TOKENS]


def cache_key(address: Any, city: Any, governorate: Any) -> str:
    return "|".join(text_norm.normalize_text(v) for v in (address, city, governorate))[:500]


def _nominatim_query(address: Any, city: Any, governorate: Any) -> str:
    parts = [str(v).strip() for v in (address, city, governorate) if v and str(v).strip()]
    unique = list(dict.fromkeys(parts))  # geocodeCity sends the city as address and city
    return ", ".join([*unique, "Tunisie"])[:MAX_QUERY_CHARS]


def _from_nominatim(item: dict[str, Any]) -> dict[str, Any]:
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    city = address.get("city") or address.get("town") or address.get("village") or address.get("suburb")
    importance = item.get("importance")
    score = float(importance) if isinstance(importance, (int, float)) else 1.0
    return {
        "lat": item["lat"],
        "lng": item["lon"],
        "match": {
            "name": item.get("name") or (item.get("display_name") or "").split(",")[0].strip() or None,
            "address": item.get("display_name"),
            "city": city,
            "governorate": address.get("state"),
            "score": js_round(score, 2),
            "source": "nominatim",
        },
    }


async def _cached(session: AsyncSession, key: str) -> GeocodeCache | None:
    row = (await session.execute(select(GeocodeCache).where(GeocodeCache.key == key))).scalar_one_or_none()
    if row is None or row.expires_at <= now_utc():
        return None
    return row


async def _remember(key: str, found: dict[str, Any] | None) -> None:
    ttl = (
        timedelta(days=settings.GEOCODE_CACHE_DAYS)
        if found
        else timedelta(hours=settings.GEOCODE_MISS_CACHE_HOURS)
    )
    values = {
        "key": key,
        "provider": "nominatim",
        "found": found is not None,
        "lat": found["lat"] if found else None,
        "lng": found["lng"] if found else None,
        "result": found["match"] if found else {},
        "expires_at": now_utc() + ttl,
    }
    try:
        async with transaction() as cache_session:
            stmt = insert(GeocodeCache).values(**values)
            await cache_session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[GeocodeCache.key],
                    set_={k: stmt.excluded[k] for k in values if k != "key"},
                )
            )
    except Exception:  # the cache is an optimization: never fail the geocode for it
        log.exception("geocode cache write failed")


async def geocode(
    session: AsyncSession, address: Any, city: Any, governorate: Any, tokens: list[str]
) -> dict[str, Any] | None:
    """{lat, lng, match} or None when nothing acceptable was found."""
    key = cache_key(address, city, governorate)
    cached = await _cached(session, key)
    ask_nominatim = True
    if cached is not None:
        if cached.found:
            return {"lat": cached.lat, "lng": cached.lng, "match": {**cached.result, "cached": True}}
        ask_nominatim = False  # Nominatim recently found nothing: straight to the places index
    if ask_nominatim:
        results = await osm.nominatim_search(_nominatim_query(address, city, governorate))
        if results:
            found = _from_nominatim(results[0])
            await _remember(key, found)
            return found
        if results is not None:  # an answer with no result (not an outage)
            await _remember(key, None)
    return await geocode_from_places(session, tokens, city, governorate)
