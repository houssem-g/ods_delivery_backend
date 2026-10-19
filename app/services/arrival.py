"""The courier's arrival time at the customer (owner, 18/10/2026: « le classement doit inclure le temps
de livraison estimé »): ONE server computation, reused by the offer sheet (getOfferEstimate), the
Commandes map's card (getRoutePreview's eta_minutes) and every offer (createOrderOffer /
updateOrderOffer store it on the offer as `estimated_eta_minutes`).

    arrival = courier → shop (his position, his vehicle)
            + the shop's average wait (the SAME for every courier of the order)
            + shop → customer (his vehicle)

* Roads: OSRM (app/integrations/osrm.py, the router the map already uses), each stretch timed at the
  courier's vehicle's pace (leg_minutes: OSRM times a car on free roads; the road km at the vehicle's
  city speed wins when slower — scooter 25 km/h, car 30, bicycle 15, walking 5, speed_kmh below).
  OSRM off, slow or empty: the straight line × ROAD_FACTOR at the same speeds.
* Wait at the shop: the mean of the last WAIT_SAMPLES real visits of that place (status « au magasin »
  → « acheté / en route », single-shop orders, last WAIT_LOOKBACK, each visit within the sample
  bounds), clamped to [WAIT_MIN, WAIT_MAX]; DEFAULT_WAIT_MINUTES (8) with fewer than
  WAIT_MIN_SAMPLES visits. A « place » = every order whose first stop is within SHOP_RADIUS_M.
* Several shops (multi-stop order, no single fixed shop): the wait counts at each shop (each its own
  average), the third line runs shop 1 → shop 2 … → customer. An unknown courier position (no GPS,
  abroad): no courier → shop line and no total (the courier types his time, nothing is « annoncé »).
* Line minutes are rounded first, the total is their sum: the three lines always add up on screen.

`ANNOUNCE_MARGIN_MINUTES`: an offer whose delay is more than this under the computation is shown
« annoncé » to the customer (his own promise, not our computation).
"""

import logging
import math
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import osrm
from app.models import Courier, Order, OrderStatusEvent, OrderStop
from app.services.geo import ORDER_BOUNDS, as_float, haversine_km, lat_of, lng_of, point, within

log = logging.getLogger("odsd.arrival")

DEFAULT_WAIT_MINUTES = 8
WAIT_MIN, WAIT_MAX = 2, 30  # the shop's average is kept inside these bounds (minutes)
SAMPLE_MIN_S, SAMPLE_MAX_S = 30, 90 * 60  # one visit outside these is noise (a test, a forgotten step)
WAIT_SAMPLES = 30
WAIT_MIN_SAMPLES = 3
WAIT_LOOKBACK = timedelta(days=90)
SHOP_RADIUS_M = 80
WAIT_TTL_S = 10 * 60
ANNOUNCE_MARGIN_MINUTES = 3
ROAD_FACTOR = 1.3  # straight line → road distance in town, when OSRM does not answer

# The courier's position: the one the app sends (his GPS, what the sheet shows him); without it, the
# last position the server has if it is recent. A sent position far from a very recent server fix
# is not his (a faked one would make him look close and dodge « annoncé »): the server's wins.
STORED_FIX_FRESH = timedelta(minutes=30)
SPOOF_FIX_FRESH = timedelta(minutes=5)
SPOOF_DISTANCE_KM = 3.0

SPEEDS_KMH = {"car": 30, "walking": 5, "bicycle": 15, "bike": 15}
DEFAULT_SPEED_KMH = 25  # scooter (and any vehicle we do not know)


def speed_kmh(vehicle: str | None) -> int:
    """Average city speed of the courier's vehicle (live tracking, route preview, arrival)."""
    return SPEEDS_KMH.get(vehicle or "", DEFAULT_SPEED_KMH)


def leg_minutes(leg: osrm.Leg, vehicle: str | None) -> float:
    """The courier's time on this stretch: OSRM times a car with free roads, so the road km at the
    vehicle's average city speed wins when it is slower (owner, 10/10/2026: « estimation pour
    livreur en scooter »)."""
    return max(leg.duration_s / 60, leg.distance_m / 1000 / speed_kmh(vehicle) * 60)


# --- OSRM trips, cached (positions rounded to ~100 m) ---------------------------------------------

TRIP_TTL_S = 15 * 60
TRIP_CACHE_MAX = 1000
trip_cache: OrderedDict[tuple, tuple[float, osrm.Trip]] = OrderedDict()


def _trip_key(points: list[tuple[float, float]]) -> tuple:
    return tuple((round(lat, 3), round(lng, 3)) for lat, lng in points)


async def road_trip(points: list[tuple[float, float]]) -> osrm.Trip | None:
    key, now = _trip_key(points), time.monotonic()
    hit = trip_cache.get(key)
    if hit is not None and now - hit[0] < TRIP_TTL_S:
        trip_cache.move_to_end(key)
        return hit[1]
    found = await osrm.trip(points)
    if found is not None:
        trip_cache[key] = (now, found)
        trip_cache.move_to_end(key)
        while len(trip_cache) > TRIP_CACHE_MAX:
            trip_cache.popitem(last=False)
    return found


def straight_trip(points: list[tuple[float, float]]) -> osrm.Trip:
    """No router: each stretch is the straight line × ROAD_FACTOR, timed by leg_minutes' speed."""
    legs = [
        osrm.Leg(duration_s=0.0, distance_m=haversine_km(a[0], a[1], b[0], b[1]) * ROAD_FACTOR * 1000)
        for a, b in pairwise(points)
    ]
    return osrm.Trip(legs=legs, coords=[], leg_coords=[[] for _ in legs])


# --- the shop's average wait -----------------------------------------------------------------------

wait_cache: dict[tuple, tuple[float, "ShopWait"]] = {}


@dataclass(frozen=True)
class ShopWait:
    minutes: int
    source: str  # "measured" | "default"
    samples: int


def _wait_of(durations_s: list[float]) -> ShopWait:
    kept = [d for d in durations_s if SAMPLE_MIN_S <= d <= SAMPLE_MAX_S][:WAIT_SAMPLES]
    if len(kept) < WAIT_MIN_SAMPLES:
        return ShopWait(DEFAULT_WAIT_MINUTES, "default", len(kept))
    mean = sum(kept) / len(kept) / 60
    return ShopWait(int(min(WAIT_MAX, max(WAIT_MIN, round(mean)))), "measured", len(kept))


async def shop_wait(session: AsyncSession, lat: float, lng: float) -> ShopWait:
    """Average minutes couriers spend at the shop at (lat, lng), from the real visits (module doc)."""
    key, now = (round(lat, 4), round(lng, 4)), time.monotonic()
    hit = wait_cache.get(key)
    if hit is not None and now - hit[0] < WAIT_TTL_S:
        return hit[1]
    other_stop = OrderStop.__table__.alias("wait_other_stop")
    first = OrderStop.__table__.alias("wait_first_stop")
    recent = list(
        (
            await session.execute(
                select(Order.id)
                .join(first, and_(first.c.order_id == Order.id, first.c.seq == 0))
                .where(
                    func.ST_DWithin(first.c.location, func.ST_GeogFromText(point(lat, lng)), SHOP_RADIUS_M),
                    Order.created_at >= func.now() - WAIT_LOOKBACK,
                    Order.resale_deal_id.is_(None),
                    ~exists().where(other_stop.c.order_id == Order.id, other_stop.c.seq > 0),
                )
                .order_by(Order.created_at.desc())
                .limit(WAIT_SAMPLES * 3)
            )
        ).scalars()
    )
    steps: dict[uuid.UUID, list[tuple[str, datetime]]] = {oid: [] for oid in recent}
    if recent:
        for oid, status, at in (
            await session.execute(
                select(OrderStatusEvent.order_id, OrderStatusEvent.to_status, OrderStatusEvent.created_at)
                .where(
                    OrderStatusEvent.order_id.in_(recent),
                    OrderStatusEvent.to_status.in_(("at_shop", "purchased", "on_the_way")),
                )
                .order_by(OrderStatusEvent.created_at, OrderStatusEvent.id)
            )
        ).all():
            steps[oid].append((status, at))
    durations = []
    for oid in recent:  # newest first
        arrived = next((at for status, at in steps[oid] if status == "at_shop"), None)
        left = next(
            (at for status, at in steps[oid] if status != "at_shop" and arrived and at > arrived), None
        )
        if arrived is not None and left is not None:
            durations.append((left - arrived).total_seconds())
    wait = _wait_of(durations)
    wait_cache[key] = (now, wait)
    if len(wait_cache) > 2000:
        wait_cache.clear()
    return wait


# --- the courier's position --------------------------------------------------------------------------


def _fresh(seen: datetime | None, within_: timedelta, now: datetime) -> bool:
    return seen is not None and now - seen <= within_


async def courier_origin(
    session: AsyncSession, courier: Courier | None, payload: dict[str, Any], now: datetime
) -> tuple[float, float] | None:
    """Where the courier starts from (module doc: the app's position, else the server's recent one)."""
    lat, lng = as_float(payload.get("from_lat")), as_float(payload.get("from_lng"))
    abroad = lat is not None and lng is not None and not within(lat, lng, ORDER_BOUNDS)
    sent = (lat, lng) if lat is not None and lng is not None and not abroad else None
    stored = None
    if courier is not None:
        row = (
            await session.execute(
                select(
                    lat_of(Courier.last_location), lng_of(Courier.last_location), Courier.last_seen_at
                ).where(Courier.id == courier.id)
            )
        ).first()
        if row is not None and row[0] is not None and within(float(row[0]), float(row[1]), ORDER_BOUNDS):
            stored = (float(row[0]), float(row[1]), row[2])
    if abroad:
        # his address abroad (shop → customer only), unless the server saw him in Tunisia moments
        # ago: then that fix (« abroad » would otherwise dodge the arrival and « annoncé »)
        return (
            (stored[0], stored[1]) if stored is not None and _fresh(stored[2], SPOOF_FIX_FRESH, now) else None
        )
    if sent is not None:
        if (
            stored is not None
            and _fresh(stored[2], SPOOF_FIX_FRESH, now)
            and haversine_km(sent[0], sent[1], stored[0], stored[1]) > SPOOF_DISTANCE_KM
        ):
            return stored[0], stored[1]
        return sent
    if stored is not None and _fresh(stored[2], STORED_FIX_FRESH, now):
        return stored[0], stored[1]
    return None


# --- the arrival -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arrival:
    vehicle: str
    source: str  # "osrm" | "straight_line"
    to_shop_km: float | None
    to_shop_min: int | None
    wait_min: int
    wait_source: str
    wait_samples: int
    shops: int
    delivery_km: float
    delivery_min: int
    trip: osrm.Trip | None = None  # the road (getRoutePreview draws it)

    @property
    def total_min(self) -> int | None:
        if self.to_shop_min is None:
            return None
        return self.to_shop_min + self.wait_min + self.delivery_min

    def as_dict(self) -> dict[str, Any]:
        return {
            "vehicle": self.vehicle,
            "source": self.source,
            "to_shop": None
            if self.to_shop_min is None
            else {"distance_km": self.to_shop_km, "minutes": self.to_shop_min},
            "wait": {
                "minutes": self.wait_min,
                "source": self.wait_source,
                "samples": self.wait_samples,
                "shops": self.shops,
            },
            "delivery": {"distance_km": self.delivery_km, "minutes": self.delivery_min},
            "total_minutes": self.total_min,
            "announce_margin_minutes": ANNOUNCE_MARGIN_MINUTES,
        }


async def order_points(
    session: AsyncSession, order_id: uuid.UUID
) -> tuple[list[tuple[float, float]], tuple[float, float]] | None:
    """The order's shops (in visiting order, those with a position) and the customer's point."""
    stops = [
        (float(r[0]), float(r[1]))
        for r in (
            await session.execute(
                select(lat_of(OrderStop.location), lng_of(OrderStop.location))
                .where(OrderStop.order_id == order_id, OrderStop.location.is_not(None))
                .order_by(OrderStop.seq)
            )
        ).all()
        if r[0] is not None
    ]
    dest = (
        await session.execute(
            select(lat_of(Order.delivery_location), lng_of(Order.delivery_location)).where(
                Order.id == order_id
            )
        )
    ).first()
    if not stops or dest is None or dest[0] is None:
        return None
    return stops, (float(dest[0]), float(dest[1]))


def _minutes(value: float) -> int:
    return max(0, math.floor(value + 0.5))  # 12.5 → 13 (round() would give 12)


async def arrival(
    session: AsyncSession,
    order_id: uuid.UUID,
    vehicle: str | None,
    origin: tuple[float, float] | None,
    *,
    road: bool = True,
) -> Arrival | None:
    """The arrival of a courier on `vehicle` starting at `origin` (None: unknown). None when the
    order has no shop or customer point. `road=False`: straight lines only (never calls OSRM)."""
    where = await order_points(session, order_id)
    if where is None:
        return None
    shops, dest = where
    points = ([origin] if origin is not None else []) + shops + [dest]
    found = await road_trip(points) if road else None
    trip = found if found is not None and len(found.legs) == len(points) - 1 else None
    legs = (trip or straight_trip(points)).legs
    to_shop = legs[0] if origin is not None else None
    after = legs[1:] if origin is not None else legs
    waits = [await shop_wait(session, lat, lng) for lat, lng in shops]
    measured = [w for w in waits if w.source == "measured"]
    return Arrival(
        vehicle=vehicle or "scooter",
        source="osrm" if trip is not None else "straight_line",
        to_shop_km=round(to_shop.distance_m / 1000, 2) if to_shop is not None else None,
        to_shop_min=_minutes(leg_minutes(to_shop, vehicle)) if to_shop is not None else None,
        wait_min=sum(w.minutes for w in waits),
        wait_source="measured" if measured and len(measured) == len(waits) else "default",
        wait_samples=min((w.samples for w in waits), default=0),
        shops=len(shops),
        delivery_km=round(sum(leg.distance_m for leg in after) / 1000, 2),
        delivery_min=max(1, _minutes(sum(leg_minutes(leg, vehicle) for leg in after))),
        trip=trip,
    )


def offer_arrival(estimated_minutes: int | None, eta_minutes: int | None) -> int | None:
    """THE arrival time of an offer (owner's rule, 19/10/2026, QA M12): the time the courier announced
    when he RAISED it above the computation, else the computation — never less than the computation
    (createOrderOffer / updateOrderOffer refuse a lower one, without any margin). Ranked on, sorted on,
    shown to the customer. No computation (his position unknown): his announced time (« non vérifié »)."""
    if estimated_minutes is None:
        return eta_minutes
    return max(estimated_minutes, eta_minutes) if eta_minutes is not None else estimated_minutes


def is_announced(eta_minutes: int | None, estimated_minutes: int | None) -> bool:
    """The courier promised clearly less than the computation: the customer reads « annoncé »."""
    return (
        eta_minutes is not None
        and estimated_minutes is not None
        and eta_minutes < estimated_minutes - ANNOUNCE_MARGIN_MINUTES
    )


async def offer_estimate(
    session: AsyncSession, order_id: uuid.UUID, courier: Courier, payload: dict[str, Any], now: datetime
) -> Arrival | None:
    """The arrival stored on an offer; never fails the offer (None when it cannot be computed, his
    position unknown included: an Arrival without its courier → shop leg has no total)."""
    try:
        async with session.begin_nested():  # a failed read never poisons the offer's transaction
            origin = await courier_origin(session, courier, payload, now)
            if origin is None:
                return None
            return await arrival(session, order_id, courier.vehicle, origin)
    except Exception:  # an estimate never blocks an offer
        log.warning("offer estimate failed", exc_info=True)
        return None
