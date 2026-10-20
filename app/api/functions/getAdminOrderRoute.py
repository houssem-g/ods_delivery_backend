"""getAdminOrderRoute — admin only, read only (owner 20/10/2026): the road route of one course, drawn
on the admin's customer / courier sheets like the map a courier sees before he bids (getRoutePreview):
shop(s) in their order → customer, by the roads (OSRM), each stretch with its distance, time and path.

Body: { order_id }. 403 for anyone but an admin, 404 for an unknown order.

Returns { success: true, source: "osrm" | "straight", vehicle, stops: [{name, address, lat, lng}],
customer: {address, lat, lng}, legs: [{distance_km, duration_min, path}], distance_km, drive_minutes,
offer: {to_shop_km, eta_announced, eta_computed} | null, courier_position: null }
or { success: false, reason: "no_location" } when the shop or the customer has no position.
`offer` is what the accepted offer recorded; the courier's position at that moment is not stored
(only his distance to the shop, `to_shop_km`), so `courier_position` stays null.
Times are the delivering courier's vehicle's (leg_minutes); a scooter when nobody delivered it.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.integrations import osrm
from app.models import Courier, Order, OrderOffer, OrderStop
from app.security.deps import CurrentUser
from app.services.arrival import leg_minutes, road_trip, straight_trip
from app.services.geo import lat_of, lng_of


def _leg(leg: osrm.Leg, path: list[list[float]], vehicle: str | None) -> dict[str, Any]:
    return {
        "distance_km": round(leg.distance_m / 1000, 2),
        "duration_min": round(leg_minutes(leg, vehicle), 1),
        "path": [[round(lat, 5), round(lng, 5)] for lat, lng in path],
    }


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if user is None or not user.is_admin:
        return 403, {"error": "Forbidden"}
    oid = as_uuid(str(payload.get("order_id") or ""))
    if oid is None:
        return 400, {"error": "Missing required field: order_id"}
    row = (
        await session.execute(
            select(
                Order.delivery_address,
                lat_of(Order.delivery_location),
                lng_of(Order.delivery_location),
                Order.courier_id,
            ).where(Order.id == oid)
        )
    ).first()
    if row is None:
        return 404, {"error": "Order not found"}
    address, c_lat, c_lng, courier_id = row
    stops = [
        {"name": name, "address": addr, "lat": float(lat), "lng": float(lng)}
        for name, addr, lat, lng in (
            await session.execute(
                select(
                    OrderStop.name, OrderStop.address, lat_of(OrderStop.location), lng_of(OrderStop.location)
                )
                .where(OrderStop.order_id == oid)
                .order_by(OrderStop.seq)
            )
        ).all()
        if lat is not None and lng is not None
    ]
    if not stops or c_lat is None or c_lng is None:
        return 200, {"success": False, "reason": "no_location"}
    customer = {"address": address, "lat": float(c_lat), "lng": float(c_lng)}

    vehicle = None
    offer = None
    if courier_id is not None:
        vehicle = await session.scalar(select(Courier.vehicle).where(Courier.id == courier_id))
        accepted = (
            await session.execute(
                select(OrderOffer.to_shop_km, OrderOffer.eta_minutes, OrderOffer.estimated_eta_minutes).where(
                    OrderOffer.order_id == oid,
                    OrderOffer.courier_id == courier_id,
                    OrderOffer.status == "accepted",
                )
            )
        ).first()
        if accepted is not None:
            offer = {
                "to_shop_km": float(accepted[0]) if accepted[0] is not None else None,
                "eta_announced": accepted[1],
                "eta_computed": accepted[2],
            }

    points = [(s["lat"], s["lng"]) for s in stops] + [(customer["lat"], customer["lng"])]
    found = await road_trip(points)
    source = "osrm"
    if found is None:
        found, source = straight_trip(points), "straight"
    legs = [_leg(leg, path, vehicle) for leg, path in zip(found.legs, found.leg_coords, strict=True)]
    return 200, {
        "success": True,
        "source": source,
        "vehicle": str(vehicle) if vehicle else "scooter",
        "stops": stops,
        "customer": customer,
        "legs": legs,
        "distance_km": round(sum(leg.distance_m for leg in found.legs) / 1000, 2),
        "drive_minutes": round(sum(leg_minutes(leg, vehicle) for leg in found.legs)),
        "offer": offer,
        "courier_position": None,
    }
