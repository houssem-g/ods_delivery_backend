"""Courier card, live position and ETA of an order (getOrderCourier, getOrderETA)."""

import math
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.integrations import osrm
from app.models import Courier, Order, OrderTracking, courier_stats
from app.services.geo import haversine_km, lat_of, lng_of
from app.services.orders import first_stop, stop_coordinates


def first_name(full_name: str | None) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


async def courier_position(
    session: AsyncSession, courier_id: uuid.UUID
) -> tuple[float, float, datetime | None] | None:
    row = (
        await session.execute(
            select(lat_of(Courier.last_location), lng_of(Courier.last_location), Courier.last_seen_at).where(
                Courier.id == courier_id
            )
        )
    ).first()
    if row is None or row[0] is None:
        return None
    return float(row[0]), float(row[1]), row[2]


async def live_position(session: AsyncSession, order_id: uuid.UUID) -> tuple[float, float, datetime] | None:
    row = (
        await session.execute(
            select(
                lat_of(OrderTracking.location), lng_of(OrderTracking.location), OrderTracking.recorded_at
            ).where(OrderTracking.order_id == order_id)
        )
    ).first()
    return (float(row[0]), float(row[1]), row[2]) if row is not None else None


async def courier_card(
    session: AsyncSession,
    courier: Courier,
    *,
    contact: bool,
    position: bool,
    order_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Public card; `contact` adds the phone and full name, `position` the freshest fix
    (the order's live position or the profile's, whichever is newer)."""
    stats = (
        await session.execute(
            select(courier_stats.c.average_rating, courier_stats.c.total_deliveries).where(
                courier_stats.c.courier_id == courier.id
            )
        )
    ).first()
    card: dict[str, Any] = {
        "id": str(courier.id),
        "full_name": courier.display_name if contact else first_name(courier.display_name),
        "first_name": first_name(courier.display_name),
        "vehicle_type": courier.vehicle,
        "average_rating": float(stats[0]) if stats and stats[0] is not None else 5,
        "total_deliveries": int(stats[1]) if stats else 0,
        "service_country": courier.service_country,
        "service_governorate": courier.service_governorate,
        "phone": (courier.phone_e164 or "") if contact else "",
        "current_lat": None,
        "current_lng": None,
        "location_updated_at": None,
    }
    if not position:
        return card
    best: tuple[float, float, datetime | None] | None = await courier_position(session, courier.id)
    if order_id is not None:
        live = await live_position(session, order_id)
        if live is not None and (best is None or best[2] is None or live[2] > best[2]):
            best = live
    if best is not None and (best[0] != 0 or best[1] != 0):
        card["current_lat"], card["current_lng"] = best[0], best[1]
        card["location_updated_at"] = legacy_datetime(best[2]) if best[2] else None
    return card


def speed_kmh(vehicle: str | None) -> int:
    if vehicle == "car":
        return 30
    if vehicle == "walking":
        return 5
    return 25


ETA_STATUSES = ("accepted", "at_shop", "purchased", "on_the_way", "client_no_response")


async def eta_destination(session: AsyncSession, order: Order) -> tuple[float, float] | None:
    """Where the courier rides to: the shop before the purchase, the customer after it. A hot-deal
    order (resale_deal_id) is bought already: straight to the customer from its acceptance."""
    if order.status in ("accepted", "at_shop") and order.resale_deal_id is None:
        return await stop_coordinates(session, await first_stop(session, order.id))
    row = (
        await session.execute(
            select(lat_of(Order.delivery_location), lng_of(Order.delivery_location)).where(
                Order.id == order.id
            )
        )
    ).first()
    return (float(row[0]), float(row[1])) if row and row[0] is not None else None


async def ride_eta_minutes(session: AsyncSession, order: Order) -> int | None:
    """Minutes left of the ride to the customer, computed like getOrderETA (what the tracking ring
    shows), never the delay the courier typed in his offer (QA B7). None when unknown."""
    try:
        answer = await order_eta(session, order, await eta_destination(session, order))
    except Exception:  # an ETA never fails a step
        return None
    eta = answer.get("eta_minutes")
    return int(eta) if isinstance(eta, (int, float)) and eta > 0 else None


async def order_eta(
    session: AsyncSession, order: Order, destination: tuple[float, float] | None
) -> dict[str, Any]:
    base = {"success": True, "order_id": str(order.id), "status": order.status}
    if order.status not in ETA_STATUSES:
        return {**base, "eta_minutes": None, "distance_km": None, "reason": "inactive_status"}
    if order.courier_id is None:
        return {**base, "eta_minutes": None, "distance_km": None, "reason": "no_courier_assigned"}
    courier = await session.get(Courier, order.courier_id)
    fixes = [await courier_position(session, order.courier_id), await live_position(session, order.id)]
    known = [f for f in fixes if f is not None]
    if not known:
        return {**base, "eta_minutes": None, "distance_km": None, "reason": "courier_location_unavailable"}
    lat, lng, _ = max(known, key=lambda f: f[2].timestamp() if f[2] else 0)
    if destination is None:
        return {**base, "eta_minutes": None, "distance_km": None, "reason": "destination_unavailable"}
    route = await osrm.route(lat, lng, destination[0], destination[1])
    if route is not None:
        return {
            **base,
            "eta_minutes": max(1, math.ceil(route.duration_s / 60)),
            "distance_km": round(route.distance_m / 1000, 2),
            "source": "osrm",
            # [[lat, lng], ...] of the road (what LiveTracking.jsx draws from its own OSRM call)
            "route_coords": route.coords,
        }
    straight = haversine_km(lat, lng, destination[0], destination[1])
    return {
        **base,
        "eta_minutes": max(1, math.ceil(straight / speed_kmh(courier.vehicle if courier else None) * 60)),
        "distance_km": round(straight, 2),
        "source": "haversine_fallback",
    }
