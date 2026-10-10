"""searchByBbox — shops and places inside the map viewport (ShopsMap).

Parameters from the query string or the JSON body (query string first):
minLat, maxLat, minLng, maxLng (required, Tunisia), category?, search_query?,
min_rating?, limit (1-100, default 50), cursor? (an offset).
Answers { success, data: { results, total_count, bbox, cursor_next } }; 400 { error }
for an invalid box. Pending shop proposals are shown to their author only.
"""

import math
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.jsnum import clamp, js_number, parse_float, parse_int
from app.security.deps import CurrentUser
from app.services.places import Bbox, BboxQuery, search_by_bbox, validate_bbox

DEFAULT_LIMIT = 50


def _param(payload: dict[str, Any], request: Request, key: str) -> str | None:
    from_query = request.query_params.get(key)
    if from_query is not None:
        return from_query
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None

    def param(key: str) -> str | None:
        return _param(payload, request, key)

    bbox = Bbox(
        min_lat=js_number(param("minLat")),
        max_lat=js_number(param("maxLat")),
        min_lng=js_number(param("minLng")),
        max_lng=js_number(param("maxLng")),
    )
    error = validate_bbox(bbox)
    if error:
        return 400, {"error": error}
    limit = clamp(parse_int(param("limit") or str(DEFAULT_LIMIT)), 1, 100)
    offset = parse_int(param("cursor") or "0")
    min_rating = parse_float(param("min_rating")) if param("min_rating") else None
    data = await search_by_bbox(
        session,
        BboxQuery(
            bbox=bbox,
            limit=DEFAULT_LIMIT if math.isnan(limit) else int(limit),
            offset=0 if math.isnan(offset) or offset < 0 else int(offset),
            category=param("category") or None,
            search_query=(param("search_query") or "")[:200] or None,
            min_rating=None if min_rating is None or math.isnan(min_rating) else min_rating,
        ),
        user,
    )
    return 200, {"success": True, "data": data}
