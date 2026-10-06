"""Live supply and demand figures for the redesigned app (Aurora, 2026-09-29).

getNetworkPulse (anyone; the Welcome screen is shown before sign-in): how many couriers are
  online around a point and how fast the first offer usually comes. Signed-in callers also get
  coarse courier dots (snapped to a 0.004° grid, about 450 m, then deduplicated: never a real
  position) and, around a shop, the couriers close to it and the fee range their tariffs give.
getDemandPulse (couriers): where and when the orders come from around the courier.

Counts only; no id, name or phone ever leaves here. QA orders ("QA TEST" / "PW-") never count.
"""

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from geoalchemy2 import Geography
from sqlalchemy import Boolean, and_, cast, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, GeocodeCache, Order, OrderOffer, OrderStop, User
from app.services import order_transitions as ot
from app.services.dispatch import DEFAULT_RADIUS_KM
from app.services.geo import ORDER_BOUNDS, as_float, haversine_km, lat_of, lng_of, within
from app.services.orders import TEST_ORDER_SQL

TUNIS = ZoneInfo("Africa/Tunis")
CITY_RADIUS_KM = 15.0
SHOP_RADIUS_KM = 1.0
NEAR_DEFAULT_KM = 3.0
NEAR_MAX_KM = 10.0
FALLBACK_CITY = "Sousse"
FIRST_OFFER_DAYS = 14
FIRST_OFFER_MIN_SAMPLES = 5
DOTS_MAX = 30
GRID_DEG = 0.004

DEMAND_WINDOW = timedelta(hours=2)
DEMAND_ZONES = 3
DEMAND_HOURS = 6
DEMAND_WEEKS = 4
DEMAND_DAYS = 14
DEMAND_MAX_KM = 50.0


def _geog(lat: float, lng: float) -> Any:
    return cast(func.ST_MakePoint(lng, lat), Geography(srid=4326))


def _point(payload: dict[str, Any], lat_key: str, lng_key: str) -> tuple[float, float] | None:
    """A point in the service country, else None (a GPS fix abroad is simply not used)."""
    lat, lng = as_float(payload.get(lat_key)), as_float(payload.get(lng_key))
    if not within(lat, lng, ORDER_BOUNDS):
        return None
    assert lat is not None and lng is not None
    return lat, lng


def _not_qa() -> Any:
    return not_(Order.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL))


def _online() -> list[Any]:
    return [
        Courier.is_online.is_(True),
        Courier.verification == "verified",
        Courier.last_location.is_not(None),
        User.deleted_at.is_(None),
    ]


def _within(column: Any, where: tuple[float, float], km: float) -> Any:
    return func.ST_DWithin(column, _geog(*where), km * 1000)


async def _count_online(session: AsyncSession, where: tuple[float, float] | None, km: float) -> int:
    stmt = select(func.count()).select_from(Courier).join(User, User.id == Courier.user_id).where(*_online())
    if where is not None:
        stmt = stmt.where(_within(Courier.last_location, where, km))
    return int((await session.execute(stmt)).scalar_one())


async def _count_reaching(session: AsyncSession, shop: tuple[float, float]) -> int:
    """Online couriers whose notification radius covers the shop: those a new order there is sent
    to (dispatch.py rule). One number for « Vérifier & publier » and « En attente d'offres » (QA B12)."""
    radius_m = func.coalesce(func.nullif(Courier.notification_radius_km, 0), DEFAULT_RADIUS_KM) * 1000
    stmt = (
        select(func.count())
        .select_from(Courier)
        .join(User, User.id == Courier.user_id)
        .where(*_online(), func.ST_DWithin(Courier.last_location, _geog(*shop), radius_m))
    )
    return int((await session.execute(stmt)).scalar_one())


async def _city(session: AsyncSession, where: tuple[float, float] | None) -> str:
    if where is None:
        return FALLBACK_CITY
    names = (
        await session.execute(
            select(func.coalesce(func.nullif(Courier.service_city, ""), Courier.service_governorate)).where(
                Courier.last_location.is_not(None),
                _within(Courier.last_location, where, CITY_RADIUS_KM),
            )
        )
    ).scalars()
    counted = Counter(n.strip() for n in names if n and n.strip())
    if counted:
        return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    cached = _geog_of_cache()
    nearest = (
        await session.execute(
            select(
                func.coalesce(
                    func.nullif(GeocodeCache.result["city"].astext, ""),
                    GeocodeCache.result["governorate"].astext,
                )
            )
            .where(
                GeocodeCache.found.is_(True),
                GeocodeCache.lat.is_not(None),
                GeocodeCache.lng.is_not(None),
                func.ST_DWithin(cached, _geog(*where), CITY_RADIUS_KM * 1000),
            )
            .order_by(func.ST_Distance(cached, _geog(*where)))
            .limit(5)
        )
    ).scalars()
    for name in nearest:
        if name and name.strip():
            return name.strip()
    return FALLBACK_CITY


def _geog_of_cache() -> Any:
    return cast(func.ST_MakePoint(GeocodeCache.lng, GeocodeCache.lat), Geography(srid=4326))


async def _first_offer_minutes(session: AsyncSession, where: tuple[float, float] | None) -> float | None:
    first = (
        select(func.min(OrderOffer.created_at))
        .where(OrderOffer.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    stmt = select(func.extract("epoch", first - Order.created_at) / 60).where(
        Order.created_at >= ot.now_utc() - timedelta(days=FIRST_OFFER_DAYS), _not_qa(), first.is_not(None)
    )
    if where is not None:
        stmt = stmt.where(_within(Order.delivery_location, where, CITY_RADIUS_KM))
    samples = [float(v) for v in (await session.execute(stmt)).scalars() if v is not None and v >= 0]
    if len(samples) < FIRST_OFFER_MIN_SAMPLES:
        return None
    return round(statistics.median(samples), 1)


def snap(lat: float, lng: float) -> tuple[float, float]:
    """The centre of the 0.004° cell of a position (privacy: the dot is never the real fix)."""
    return (
        round((math.floor(lat / GRID_DEG) + 0.5) * GRID_DEG, 4),
        round((math.floor(lng / GRID_DEG) + 0.5) * GRID_DEG, 4),
    )


async def _dots(session: AsyncSession, where: tuple[float, float], km: float) -> list[dict[str, float]]:
    rows = (
        await session.execute(
            select(lat_of(Courier.last_location), lng_of(Courier.last_location))
            .join(User, User.id == Courier.user_id)
            .where(*_online(), _within(Courier.last_location, where, km))
            .order_by(func.ST_Distance(Courier.last_location, _geog(*where)))
            .limit(200)
        )
    ).all()
    seen: dict[tuple[float, float], None] = {}
    for lat, lng in rows:
        seen.setdefault(snap(float(lat), float(lng)), None)
        if len(seen) >= DOTS_MAX:
            break
    return [{"lat": lat, "lng": lng} for lat, lng in seen]


def _half(value: Decimal) -> float:
    return float((value * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2)


async def _fee_range(
    session: AsyncSession, shop: tuple[float, float], where: tuple[float, float] | None, km: float
) -> dict[str, float] | None:
    """What the online couriers around the shop would ask: the app's suggested price of each
    (src/lib/orderFlow.js computeOfferQuote: his tariff × (him → shop + shop → customer), his
    minimum, 1 DT at least), so the customer's « ≈ » matches the offers he gets (QA B8)."""
    if where is None:
        return None
    rows = (
        await session.execute(
            select(
                Courier.price_per_km,
                Courier.min_fee,
                lat_of(Courier.last_location),
                lng_of(Courier.last_location),
            )
            .join(User, User.id == Courier.user_id)
            .where(*_online(), _within(Courier.last_location, shop, km))
        )
    ).all()
    if not rows:
        return None
    ride = haversine_km(*shop, *where)
    fees = []
    for price, min_fee, lat, lng in rows:
        to_shop = haversine_km(float(lat), float(lng), *shop) if lat is not None else 0.0
        distance = Decimal(str(round(to_shop + ride, 3)))
        fees.append(_half(max(Decimal(1), min_fee or Decimal(0), distance * (price or Decimal(0)))))
    return {"min": min(fees), "max": max(fees)}


async def network_pulse(session: AsyncSession, payload: dict[str, Any], signed_in: bool) -> dict[str, Any]:
    where = _point(payload, "lat", "lng")
    radius = as_float(payload.get("radius_km"))
    km = NEAR_DEFAULT_KM if radius is None or radius <= 0 else min(radius, NEAR_MAX_KM)
    body: dict[str, Any] = {
        "success": True,
        "city": await _city(session, where),
        "online_city": await _count_online(session, where, CITY_RADIUS_KM),
        "online_near": await _count_online(session, where, km) if where is not None else None,
        "radius_km": km,
        "first_offer_minutes": await _first_offer_minutes(session, where),
    }
    if not signed_in:
        return body
    body["courier_dots"] = await _dots(session, where, km) if where is not None else []
    shop = _point(payload, "shop_lat", "shop_lng")
    if shop is not None:
        body["couriers_near_shop"] = await _count_online(session, shop, SHOP_RADIUS_KM)
        body["couriers_for_shop"] = await _count_reaching(session, shop)
        body["fee_range"] = await _fee_range(session, shop, where, km)
    return body


# ─────────────────────────── getDemandPulse ───────────────────────────


def _area() -> Any:
    shop_city = (
        select(func.coalesce(func.nullif(OrderStop.city, ""), OrderStop.governorate))
        .where(OrderStop.order_id == Order.id, OrderStop.seq == 0)
        .correlate(Order)
        .scalar_subquery()
    )
    return func.coalesce(func.nullif(Order.delivery_city, ""), shop_city)


async def _zones(session: AsyncSession, where: tuple[float, float], km: float, now: datetime) -> list[dict]:
    area = _area().label("area")
    near = and_(Order.delivery_location.is_not(None), _within(Order.delivery_location, where, km), _not_qa())
    recent = or_(Order.created_at >= now - DEMAND_WINDOW, Order.status.in_(ot.OPEN_STATUSES))
    rows = (
        await session.execute(
            select(
                area,
                func.count(),
                func.avg(lat_of(Order.delivery_location)),
                func.avg(lng_of(Order.delivery_location)),
            )
            .where(near, recent, Order.created_at >= now - timedelta(days=2), _area().is_not(None))
            .group_by(area)
            .order_by(func.count().desc(), area)
            .limit(DEMAND_ZONES)
        )
    ).all()
    if not rows:
        return []
    names = [r[0] for r in rows]
    week_ago = now - timedelta(days=7)
    before = dict(
        (
            await session.execute(
                select(area, func.count())
                .where(
                    near,
                    Order.created_at >= week_ago - DEMAND_WINDOW,
                    Order.created_at < week_ago,
                    _area().in_(names),
                )
                .group_by(area)
            )
        ).all()
    )
    zones = []
    for name, count, lat, lng in rows:
        old = int(before.get(name) or 0)
        zones.append(
            {
                "name": name,
                "lat": round(float(lat), 4),
                "lng": round(float(lng), 4),
                "count": int(count),
                "trend_pct": round(100 * (int(count) - old) / old) if old else None,
            }
        )
    return zones


async def _hourly(
    session: AsyncSession, where: tuple[float, float], km: float, now: datetime
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    local_hour = func.date_trunc("hour", func.timezone("Africa/Tunis", Order.created_at)).label("h")
    since = now - timedelta(days=7 * DEMAND_WEEKS + 1)
    counts: dict[datetime, int] = defaultdict(int)
    for hour, n in (
        await session.execute(
            select(local_hour, func.count())
            .where(
                Order.created_at >= since,
                Order.delivery_location.is_not(None),
                _within(Order.delivery_location, where, km),
                _not_qa(),
            )
            .group_by(local_hour)
        )
    ).all():
        counts[hour.replace(tzinfo=None)] = int(n)
    start = now.astimezone(TUNIS).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hourly = []
    for step in range(DEMAND_HOURS):
        slot = start + timedelta(hours=step)
        total = sum(counts.get(slot - timedelta(days=7 * week), 0) for week in range(1, DEMAND_WEEKS + 1))
        hourly.append({"hour": slot.hour, "avg": round(total / DEMAND_WEEKS, 1)})
    best, peak = 0.0, None
    for i in range(len(hourly) - 1):
        pair = hourly[i]["avg"] + hourly[i + 1]["avg"]
        if pair > best:
            best, peak = pair, {"from": hourly[i]["hour"], "to": (hourly[i]["hour"] + 2) % 24}
    return hourly, peak


async def _orders_per_day(
    session: AsyncSession, where: tuple[float, float], km: float, now: datetime
) -> float:
    count = (
        await session.execute(
            select(func.count()).where(
                Order.created_at >= now - timedelta(days=DEMAND_DAYS),
                Order.delivery_location.is_not(None),
                _within(Order.delivery_location, where, km),
                _not_qa(),
            )
        )
    ).scalar_one()
    return round(int(count) / DEMAND_DAYS, 1)


async def demand_pulse(
    session: AsyncSession, courier: Courier, where: tuple[float, float], radius_km: float | None
) -> dict[str, Any]:
    km = radius_km if radius_km is not None and radius_km > 0 else float(courier.notification_radius_km or 0)
    km = min(max(km, 1.0), DEMAND_MAX_KM)
    now = ot.now_utc()
    hourly, peak = await _hourly(session, where, km, now)
    return {
        "success": True,
        "radius_km": km,
        "zones": await _zones(session, where, km, now),
        "hourly": hourly,
        "peak": peak,
        "orders_per_day": await _orders_per_day(session, where, km, now),
    }


def point_of(payload: dict[str, Any]) -> tuple[float, float] | None:
    return _point(payload, "lat", "lng")
