"""Small geographic helpers shared by the order services (same formulas as the Deno functions)."""

import math
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import cast, func

EARTH_RADIUS_KM = 6371.0

# Tunisia with a margin (placeOrder: the map and the geocoder are limited to it).
ORDER_BOUNDS = (29.5, 38.5, 7.0, 12.5)
# trackCourierLocation's box (GEO_VALIDATION).
TRACKING_BOUNDS = (30.0, 37.5, 8.0, 12.5)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def as_float(value: Any) -> float | None:
    """A finite number from a JSON value (numbers and numeric strings), else None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def within(lat: float | None, lng: float | None, bounds: tuple[float, float, float, float]) -> bool:
    if lat is None or lng is None:
        return False
    min_lat, max_lat, min_lng, max_lng = bounds
    return min_lat <= lat <= max_lat and min_lng <= lng <= max_lng


def point(lat: float, lng: float) -> str:
    """EWKT for a geography(Point, 4326) column."""
    return f"SRID=4326;POINT({lng} {lat})"


def lat_of(column: Any) -> Any:
    return func.ST_Y(cast(column, Geometry))


def lng_of(column: Any) -> Any:
    return func.ST_X(cast(column, Geometry))
