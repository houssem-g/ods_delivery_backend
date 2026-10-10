"""searchPlaces — places around a point (map explorer, address / shop pickers).

Payload: { query?, category?, governorate?, city?, lat, lng, radius_km = 8, limit = 20 }.
Answers { success, places, total } — each place in the PlaceIndex record shape plus
`distance_km` and `score`, best first. 400 { error: 'Location required' } without a
numeric lat / lng. Bounds: radius 0.1-100 km, 1-200 results, 12 query
tokens of the first 200 characters. See app/services/places.py for the scoring.
"""

import math
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.jsnum import clamp, is_finite_number, number_or
from app.security.deps import CurrentUser
from app.services import text_norm
from app.services.places import PlaceQuery, search_places


def _text(value: Any) -> str | None:
    return str(value) if value else None


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    lat, lng = payload.get("lat"), payload.get("lng")
    if not is_finite_number(lat) or not is_finite_number(lng):
        return 400, {"error": "Location required"}
    query = payload.get("query") or ""
    radius_km = clamp(number_or(payload.get("radius_km", 8), 8), 0.1, 100)
    max_results = math.trunc(clamp(number_or(payload.get("limit", 20), 20), 1, 200))
    found = await search_places(
        session,
        PlaceQuery(
            lat=float(lat),
            lng=float(lng),
            radius_km=radius_km,
            tokens=text_norm.tokenize(str(query)[:200])[:12],
            max_results=max_results,
            category=_text(payload.get("category")),
            governorate=_text(payload.get("governorate")),
            city=_text(payload.get("city")),
        ),
    )
    return 200, {"success": True, "places": found, "total": len(found)}
