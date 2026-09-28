"""Place search over `places` (ex-PlaceIndex) and `shops`: ports of searchPlaces,
searchByBbox and the places side of geocodeAddress.

Base44 loaded the 1 000-4 000 "best" PlaceIndex rows and filtered them in memory, so
anything outside that slice was invisible. Here the radius / bounding box is a PostGIS
query over every place, and the Deno text scores are computed in SQL:

- a query token matches a place when it is a substring of `places.search_norm`
  (normalized name, address, city: `LIKE '%token%'`, trigram index) or of the place's
  normalized category (the category vocabulary is small: matched in Python, then
  `category IN (...)`);
- scores, filters, rounding and sort keys follow the Deno code, so result order is the same.

Deliberate difference: the governorate / city filters only drop places whose governorate
/ city is *known and different*. On Base44 they required an exact match although 19 of
the 10 705 places had a governorate, which emptied most searches (the radius already
bounds the area).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import ColumnElement, Float, Numeric, and_, case, cast, func, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import Place, Shop
from app.security.deps import CurrentUser
from app.services import text_norm

places = Place.__table__
shops = Shop.__table__

EARTH_RADIUS_M = 6_371_000.0
DEFAULT_QUALITY = 0.6  # Deno: Number(place.quality_score || 0.6)


def js_round(value: float, digits: int = 0) -> float:
    """`Math.round(value * 10**digits) / 10**digits` (half up, unlike Python's round)."""
    factor = 10**digits
    return math.floor(value * factor + 0.5) / factor


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    to_rad = math.radians
    d_lat, d_lng = to_rad(lat2 - lat1), to_rad(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def lat_expr(column: Any) -> ColumnElement[float]:
    return func.ST_Y(cast(column, Geometry))


def lng_expr(column: Any) -> ColumnElement[float]:
    return func.ST_X(cast(column, Geometry))


def point(lat: float, lng: float) -> ColumnElement[Any]:
    return func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326).cast(places.c.location.type)


def quality_fraction(column: Any = places.c.quality_score) -> ColumnElement[float]:
    """quality_score (percent) as the 0-1 number Base44 stored; NULL stays NULL."""
    return cast(column, Float) / 100.0


def legacy_quality(value: int | None) -> float | None:
    return None if value is None else value / 100.0


# --- vocabulary filters ---------------------------------------------------------------


async def _distinct(session: AsyncSession, column: Any) -> list[str]:
    rows = await session.execute(select(column).where(column.is_not(None)).distinct())
    return [value for (value,) in rows]


def _known_and_equal(column: Any, values: Sequence[str]) -> ColumnElement[bool]:
    """Unknown (NULL / '') passes; a known value must be one of `values`."""
    unknown = func.coalesce(column, "") == ""
    return or_(unknown, column.in_(values)) if values else unknown


async def lenient_filter(session: AsyncSession, column: Any, requested: Any) -> ColumnElement[bool]:
    """governorate / city filter: normalized equality, places without the value kept."""
    wanted = text_norm.normalize_text(requested) if requested else ""
    if not wanted:
        return true()
    matching = [v for v in await _distinct(session, column) if text_norm.normalize_text(v) == wanted]
    return _known_and_equal(column, matching)


def same_category(place_category: Any, requested: Any) -> bool:
    """Deno searchPlaces `sameCategory` (an empty place category matches everything)."""
    if not requested:
        return True
    place_norm = text_norm.normalize_text(place_category)
    requested_norm = text_norm.normalize_text(requested)
    return requested_norm in place_norm or place_norm in requested_norm


CATEGORY_ALIASES: dict[str, list[str]] = {
    "restaurant": ["restaurant", "resto", "fast_food", "snack", "food", "pizza", "grill"],
    "pharmacie": ["pharmacie", "pharmacy"],
    "supermarche": ["supermarche", "supermarket", "convenience", "grocery"],
    "boulangerie": ["boulangerie", "bakery", "patisserie"],
    "banque": ["banque", "bank"],
    "carburant": ["carburant", "fuel", "station"],
    "hopital": ["hopital", "hospital", "clinic", "clinique"],
}


def bbox_category_matches(value: Any, requested: Any) -> bool:
    """Deno searchByBbox `matchesCategory`."""
    if not requested:
        return True
    requested_norm = text_norm.fold_trim(requested)
    value_norm = text_norm.fold_trim(value)
    return any(token in value_norm for token in CATEGORY_ALIASES.get(requested_norm, [requested_norm]))


def _categories_where(values: Iterable[str], keep: Any) -> list[str]:
    return [v for v in values if keep(v)]


async def _token_matchers(
    session: AsyncSession, tokens: Sequence[str], empty_category_as: str = ""
) -> list[ColumnElement[bool]]:
    """One SQL condition per token: substring of search_norm or of the category."""
    categories = await _distinct(session, places.c.category)
    matchers = []
    for token in tokens:
        in_category = _categories_where(
            categories, lambda c, t=token: t in text_norm.normalize_text(c or empty_category_as)
        )
        text_match = places.c.search_norm.contains(token, autoescape=True)
        matchers.append(or_(text_match, places.c.category.in_(in_category)) if in_category else text_match)
    return matchers


# --- documents ----------------------------------------------------------------------------


def place_columns() -> list[Any]:
    return [
        places.c.id,
        places.c.osm_id,
        places.c.name,
        places.c.name_norm,
        places.c.address,
        places.c.city,
        places.c.governorate,
        places.c.category,
        lat_expr(places.c.location).label("lat"),
        lng_expr(places.c.location).label("lng"),
        places.c.phone,
        places.c.source,
        places.c.opening_hours,
        places.c.source_ts,
        places.c.quality_score,
        places.c.created_at,
        places.c.updated_at,
    ]


def place_document(row: Any) -> dict[str, Any]:
    """A place in the legacy PlaceIndex record shape."""
    m = row._mapping
    return {
        "id": str(m["id"]),
        "created_date": legacy_datetime(m["created_at"]),
        "updated_date": legacy_datetime(m["updated_at"]),
        "created_by": None,
        "osm_id": m["osm_id"],
        "name": m["name"],
        "name_norm": m["name_norm"],
        "address": m["address"],
        "city": m["city"],
        "governorate": m["governorate"],
        "category": m["category"],
        "lat": m["lat"],
        "lng": m["lng"],
        "phone": m["phone"],
        "source": m["source"],
        "opening_hours": m["opening_hours"],
        "source_ts": legacy_datetime(m["source_ts"]),
        "quality_score": legacy_quality(m["quality_score"]),
    }


# --- searchPlaces -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaceQuery:
    lat: float
    lng: float
    radius_km: float
    tokens: list[str]
    max_results: int
    category: str | None = None
    governorate: str | None = None
    city: str | None = None


def _dedupe_key(doc: dict[str, Any]) -> str:
    return f"{text_norm.normalize_text(doc['name'])}|{doc['lat']:.4f}|{doc['lng']:.4f}"


async def search_places(session: AsyncSession, q: PlaceQuery) -> list[dict[str, Any]]:
    """searchPlaces: places within the radius, scored 0.45 text + 0.25 distance + 0.2
    category + 0.1 quality, best first (then nearest), deduplicated by name + position."""
    center = point(q.lat, q.lng)
    distance_m = func.ST_Distance(places.c.location, center, False)
    distance_km_2 = func.round(cast(distance_m / 1000.0, Numeric), 2)

    if q.tokens:
        matchers = await _token_matchers(session, q.tokens)
        matched = sum((case((m, 1), else_=0) for m in matchers), start=literal(0))
        text_score = cast(matched, Float) / float(len(q.tokens))
    else:
        text_score = literal(0.5, Float)
    distance_score = func.greatest(0.0, 1.0 - (distance_m / 1000.0) / max(q.radius_km, 1.0))
    quality = func.least(
        1.0, func.greatest(0.0, func.coalesce(quality_fraction(func.nullif(places.c.quality_score, 0)), 0.6))
    )
    # The category filter below keeps only matching places: its score is always 1.
    raw_score = 0.45 * text_score + 0.25 * distance_score + 0.2 + 0.1 * quality
    score = func.round(cast(raw_score * 1000.0, Numeric)) / 1000.0

    where = [
        func.ST_DWithin(places.c.location, center, q.radius_km * 1000.0 + 10.0, False),
        distance_km_2 <= q.radius_km,
        await lenient_filter(session, places.c.governorate, q.governorate),
        await lenient_filter(session, places.c.city, q.city),
    ]
    if q.category:
        categories = await _distinct(session, places.c.category)
        where.append(
            places.c.category.in_(_categories_where(categories, lambda c: same_category(c, q.category)))
        )
    # (Deno also dropped scores < 0.2 when a text was given: the category term alone is 0.2.)

    stmt = (
        select(*place_columns(), distance_km_2.label("distance_km"), score.label("score"))
        .where(*where)
        .order_by(score.desc(), distance_km_2.asc(), places.c.id.asc())
    )
    batch = max(q.max_results * 2, 50)
    offset = 0
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    while len(found) < q.max_results:
        rows = (await session.execute(stmt.limit(batch).offset(offset))).all()
        for row in rows:
            doc = place_document(row)
            doc["distance_km"] = float(row.distance_km)
            doc["score"] = float(row.score)
            key = _dedupe_key(doc)
            if key in seen:
                continue
            seen.add(key)
            found.append(doc)
            if len(found) == q.max_results:
                break
        if len(rows) < batch:
            break
        offset += batch
    return found


# --- searchByBbox --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bbox:
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.min_lat + self.max_lat) / 2, (self.min_lng + self.max_lng) / 2

    def contains(self, lat: float, lng: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lng <= lng <= self.max_lng

    def as_dict(self) -> dict[str, float]:
        return {
            "minLat": self.min_lat,
            "maxLat": self.max_lat,
            "minLng": self.min_lng,
            "maxLng": self.max_lng,
        }


def validate_bbox(bbox: Bbox) -> str | None:
    """Deno `validateBbox`: the error message, or None."""
    if not (math.isfinite(bbox.min_lat) and math.isfinite(bbox.max_lat)):
        return "Invalid latitude values"
    if not (math.isfinite(bbox.min_lng) and math.isfinite(bbox.max_lng)):
        return "Invalid longitude values"
    if bbox.min_lat >= bbox.max_lat:
        return "minLat must be less than maxLat"
    if bbox.min_lng >= bbox.max_lng:
        return "minLng must be less than maxLng"
    if bbox.max_lat < 30 or bbox.min_lat > 37.5 or bbox.max_lng < 8 or bbox.min_lng > 12.5:
        return "Bounding box outside Tunisia"
    return None


@dataclass(frozen=True)
class BboxQuery:
    bbox: Bbox
    limit: int
    offset: int = 0
    category: str | None = None
    search_query: str | None = None
    min_rating: float | None = None


def _in_bbox_where(column: Any, bbox: Bbox) -> list[ColumnElement[bool]]:
    """Index-backed circle around the box, then the exact latitude/longitude bounds."""
    lat_c, lng_c = bbox.center
    half_diagonal = haversine_m(lat_c, lng_c, bbox.max_lat, bbox.max_lng)
    return [
        func.ST_DWithin(column, point(lat_c, lng_c), half_diagonal * 1.02 + 100.0, False),
        lat_expr(column).between(bbox.min_lat, bbox.max_lat),
        lng_expr(column).between(bbox.min_lng, bbox.max_lng),
    ]


def _bbox_result(bbox: Bbox, item: dict[str, Any]) -> dict[str, Any]:
    lat_c, lng_c = bbox.center
    result = {"id": item["id"]}
    if item.get("shop_id") is not None:
        result["shop_id"] = item["shop_id"]
    result.update(
        {
            "source": item["source"],
            "name": item["name"],
            "category": item["category"] or "restaurant",
            "lat": item["lat"],
            "lng": item["lng"],
            "rating": item["rating"] or 0,
            "review_count": item["review_count"] or 0,
            "distance_meters": int(js_round(haversine_m(lat_c, lng_c, item["lat"], item["lng"]))),
            "phone": item["phone"],
            "address": item["address"],
            "city": item["city"],
        }
    )
    return result


def _shop_visible(viewer: CurrentUser) -> ColumnElement[bool]:
    """Proposals are shown to their author only until approved; rejected ones never."""
    return or_(
        shops.c.review_status == "approved",
        and_(shops.c.review_status == "pending", shops.c.proposed_by == viewer.id),
    )


async def _bbox_shops(session: AsyncSession, q: BboxQuery, viewer: CurrentUser) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                shops.c.id,
                shops.c.name,
                shops.c.categories,
                shops.c.phone,
                shops.c.address,
                shops.c.city,
                lat_expr(shops.c.location).label("lat"),
                lng_expr(shops.c.location).label("lng"),
            ).where(_shop_visible(viewer), *_in_bbox_where(shops.c.location, q.bbox))
        )
    ).all()
    tokens = text_norm.tokenize(q.search_query)
    items = []
    for row in rows:
        category = (row.categories[0] if row.categories else None) or "restaurant"
        item = {
            "id": f"shop:{row.id}",
            "shop_id": str(row.id),
            "source": "shop",
            "name": row.name or "Shop",
            "category": category,
            "lat": row.lat,
            "lng": row.lng,
            "rating": 0,  # Shop has no rating / review_count: always 0 on Base44 too
            "review_count": 0,
            "phone": row.phone or "",
            "address": row.address or "",
            "city": row.city or "",
        }
        if not bbox_category_matches(category, q.category):
            continue
        if q.min_rating:
            continue  # rating 0 never passes a minimum
        haystack = text_norm.fold_trim(" ".join([item["name"], item["address"], item["city"], category]))
        if all(token in haystack for token in tokens):
            items.append(item)
    return items


async def _bbox_place_filters(session: AsyncSession, q: BboxQuery) -> list[ColumnElement[bool]]:
    where = _in_bbox_where(places.c.location, q.bbox)
    rating = func.coalesce(quality_fraction(), 0.0)
    if q.category:
        categories = await _distinct(session, places.c.category)
        where.append(
            places.c.category.in_(
                _categories_where(categories, lambda c: bbox_category_matches(c or "place", q.category))
            )
        )
    if q.min_rating:
        where.append(rating >= q.min_rating)
        where.append(rating > 0)
    tokens = text_norm.tokenize(q.search_query)
    if tokens:
        where.extend(await _token_matchers(session, tokens, empty_category_as="place"))
    return where


async def search_by_bbox(session: AsyncSession, q: BboxQuery, viewer: CurrentUser) -> dict[str, Any]:
    """searchByBbox: shops and places inside the map viewport, best rated first, then
    nearest to the centre; `cursor` = offset into that order."""
    shop_items = await _bbox_shops(session, q, viewer)
    where = await _bbox_place_filters(session, q)
    rating = func.coalesce(quality_fraction(), 0.0)
    lat_c, lng_c = q.bbox.center
    distance = func.ST_Distance(places.c.location, point(lat_c, lng_c), False)
    place_total = (await session.execute(select(func.count()).select_from(places).where(*where))).scalar_one()
    wanted = q.offset + q.limit
    rows = (
        await session.execute(
            select(*place_columns(), rating.label("rating"))
            .where(*where)
            .order_by(rating.desc(), distance.asc(), places.c.id.asc())
            .limit(wanted)
        )
    ).all()
    place_items = [
        {
            "id": f"place_index:{row.id}",
            "source": "place_index",
            "name": row.name or "Place",
            "category": row.category or "place",
            "lat": row.lat,
            "lng": row.lng,
            "rating": float(row.rating),
            "review_count": 0,
            "phone": row.phone or "",
            "address": row.address or "",
            "city": row.city or "",
        }
        for row in rows
    ]
    merged = [_bbox_result(q.bbox, item) for item in shop_items + place_items]
    merged.sort(key=lambda r: (-r["rating"], -r["review_count"], r["distance_meters"]))
    total = len(shop_items) + place_total
    page = merged[q.offset : wanted]
    return {
        "results": page,
        "total_count": total,
        "bbox": q.bbox.as_dict(),
        "cursor_next": str(wanted) if wanted < total else None,
    }


# --- geocodeAddress, places side ---------------------------------------------------------------

GEOCODE_MIN_SCORE = 0.34
GEOCODE_CANDIDATES = 2000


async def geocode_from_places(
    session: AsyncSession, tokens: list[str], city: str | None, governorate: str | None
) -> dict[str, Any] | None:
    """Deno geocodeAddress: best place by token similarity on name + address + city,
    +0.15 when the city matches, + up to 0.1 for quality; accepted from 0.34."""
    if not tokens:
        return None
    matchers = [places.c.search_norm.contains(token, autoescape=True) for token in tokens]
    city_norm = text_norm.normalize_text(city) if city else ""
    quality = func.coalesce(quality_fraction(), 0.0)
    approx = sum((case((m, 1), else_=0) for m in matchers), start=literal(0))
    rows = (
        await session.execute(
            select(*place_columns(), places.c.search_norm)
            .where(or_(*matchers), await lenient_filter(session, places.c.governorate, governorate))
            .order_by(approx.desc(), quality.desc(), places.c.id.asc())
            .limit(GEOCODE_CANDIDATES)
        )
    ).all()
    best: tuple[float, Any] | None = None
    for row in rows:
        hay = row.search_norm or text_norm.search_text(row.name, row.address, row.city)
        score = sum(1 for token in tokens if token in hay) / len(tokens)
        if city_norm and text_norm.normalize_text(row.city) == city_norm:
            score += 0.15
        q = legacy_quality(row.quality_score) or 0.0
        score += max(0.0, min(0.1, q * 0.1))
        if best is None or score > best[0]:
            best = (score, row)
    if best is None or best[0] < GEOCODE_MIN_SCORE:
        return None
    score, row = best
    return {
        "lat": row.lat,
        "lng": row.lng,
        "match": {
            "name": row.name,
            "address": row.address,
            "city": row.city,
            "governorate": row.governorate,
            "score": js_round(score, 2),
            "source": "place_index",
        },
    }


# --- maintenance ------------------------------------------------------------------------------


async def backfill_search_norm(session: AsyncSession, batch: int = 1000) -> int:
    """Computes name_norm / search_norm where they are missing (rows imported from Base44:
    4 706 had no name_norm, none has search_norm). Returns the number of rows fixed."""
    fixed = 0
    while True:
        rows = (
            await session.execute(
                select(places.c.id, places.c.name, places.c.address, places.c.city)
                .where(places.c.search_norm == "")
                .order_by(places.c.id)
                .limit(batch)
            )
        ).all()
        if not rows:
            return fixed
        for row in rows:
            search = text_norm.search_text(row.name, row.address, row.city)
            await session.execute(
                places.update()
                .where(places.c.id == row.id)
                .values(
                    name_norm=text_norm.normalize_text(row.name),
                    # a name without letters/digits keeps a marker so it is not picked again
                    search_norm=search or "-",
                )
            )
        fixed += len(rows)
        if len(rows) < batch:
            return fixed
