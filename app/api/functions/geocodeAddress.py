"""geocodeAddress — an address (+ city, governorate) → coordinates.

Payload: { address, city?, governorate? }. Answers
{ lat, lng, found: true, geocode_status: 'resolved', match: {name, address, city,
governorate, score, source} } (source 'nominatim' or 'place_index'), or
404 { found: false, geocode_status: 'failed', error } so the caller asks for a pin on
the map. 400 { error: 'Missing address' }; 400 'Empty query' when the address has no
letter or digit. Cache → Nominatim → places index: app/services/geocode.py.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services.geocode import geocode, query_tokens

NOT_FOUND = "No match for this address. Ask the user to pick a location on the map."


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    address, city, governorate = payload.get("address"), payload.get("city"), payload.get("governorate")
    if not address:
        return 400, {"error": "Missing address"}
    tokens = query_tokens(address, city)
    if not tokens:
        return 400, {"found": False, "geocode_status": "failed", "error": "Empty query"}
    found = await geocode(session, address, city, governorate, tokens)
    if found is None:
        return 404, {"found": False, "geocode_status": "failed", "error": NOT_FOUND}
    return 200, {
        "lat": found["lat"],
        "lng": found["lng"],
        "found": True,
        "geocode_status": "resolved",
        "match": found["match"],
    }
