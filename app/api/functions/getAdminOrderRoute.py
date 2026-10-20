"""getAdminOrderRoute — admin only, read only (owner 20/10/2026): the road route of one course, drawn
on the admin's customer / courier sheets like the map a courier sees before he bids (getRoutePreview):
shop(s) in their order → customer, by the roads (OSRM), each stretch with its distance, time and path.

Body: { order_id }. 403 for anyone but an admin, 404 for an unknown order.

Returns { success: true, source: "osrm" | "straight", vehicle, stops: [{name, address, lat, lng}],
customer: {address, lat, lng}, legs: [{distance_km, duration_min, path}], distance_km, drive_minutes,
offer: {to_shop_km, eta_announced, eta_computed} | null,
courier_position: {lat, lng, at} | null, to_shop: {distance_km, duration_min, path} | null,
other_offers: [{lat, lng, at, status}], steps: [{key, at, minutes, lat, lng}],
incidents: [{kind, at, lat, lng}] }
or { success: false, reason: "no_location", steps, incidents } when the shop or the customer has no position.
`offer` is what the accepted offer recorded; `courier_position` is where its courier was when he sent /
last changed it (order_offers.courier_lat/lng, owner 20/10/2026 — null for older offers: only the
distance `to_shop_km` then) and `to_shop` the road from there to the first shop (not in `legs`, nor in
`distance_km` / `drive_minutes`, which stay the shop(s) → customer course). `other_offers`: the positions
the order's other offers recorded. Admin only: these positions are never served to customers or couriers.
`steps` (owner 20/10/2026): the course's steps from the status history (order_status_events), always the
five keys of STEPS in order — accepted → at_shop → purchased → on_the_way → delivered — from the LAST
acceptance (a course handed to another courier restarts there); a step not in the history has `at` null.
`minutes`: since the previous step that happened. `lat`/`lng`: the position the courier app sent with that
step, null without one. `incidents`: « article indisponible » (order_stock_checks), price to confirm, customer
not answering, cancellation, reported issue — in time order. Nothing is invented: only what was recorded.
Times are the delivering courier's vehicle's (leg_minutes); a scooter when nobody delivered it.
"""

from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.integrations import osrm
from app.models import Courier, Order, OrderIssue, OrderOffer, OrderStatusEvent, OrderStockCheck, OrderStop
from app.security.deps import CurrentUser
from app.services.arrival import leg_minutes, road_trip, straight_trip
from app.services.geo import lat_of, lng_of


def _leg(leg: osrm.Leg, path: list[list[float]], vehicle: str | None) -> dict[str, Any]:
    return {
        "distance_km": round(leg.distance_m / 1000, 2),
        "duration_min": round(leg_minutes(leg, vehicle), 1),
        "path": [[round(lat, 5), round(lng, 5)] for lat, lng in path],
    }


def _position(lat: Any, lng: Any, at: datetime | None) -> dict[str, Any] | None:
    if lat is None or lng is None:
        return None
    return {"lat": float(lat), "lng": float(lng), "at": at.isoformat() if at is not None else None}


STEPS = ("accepted", "at_shop", "purchased", "on_the_way", "delivered")
INCIDENT_STATUSES = ("price_confirmation_needed", "client_no_response", "cancelled")


def _point(lat: Any, lng: Any) -> dict[str, float | None]:
    if lat is None or lng is None:
        return {"lat": None, "lng": None}
    return {"lat": round(float(lat), 5), "lng": round(float(lng), 5)}


async def _steps(session: AsyncSession, oid: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = (
        await session.execute(
            select(
                OrderStatusEvent.to_status,
                OrderStatusEvent.created_at,
                lat_of(OrderStatusEvent.location),
                lng_of(OrderStatusEvent.location),
            )
            .where(OrderStatusEvent.order_id == oid)
            .order_by(OrderStatusEvent.created_at, OrderStatusEvent.id)
        )
    ).all()
    starts = [i for i, e in enumerate(events) if str(e[0]) == "accepted"]
    course = events[starts[-1] :] if starts else events
    found: dict[str, Any] = {}
    for status, at, lat, lng in course:
        if str(status) in STEPS and str(status) not in found:
            found[str(status)] = (at, lat, lng)
    steps, previous = [], None
    for key in STEPS:
        at, lat, lng = found.get(key, (None, None, None))
        minutes = (
            round((at - previous).total_seconds() / 60) if at is not None and previous is not None else None
        )
        steps.append(
            {"key": key, "at": at.isoformat() if at else None, "minutes": minutes, **_point(lat, lng)}
        )
        previous = at or previous
    incidents = [
        {"kind": str(status), "at": at, **_point(lat, lng)}
        for status, at, lat, lng in events
        if str(status) in INCIDENT_STATUSES
    ]
    incidents += [
        {"kind": "item_unavailable", "at": at, **_point(None, None)}
        for (at,) in (
            await session.execute(select(OrderStockCheck.created_at).where(OrderStockCheck.order_id == oid))
        ).all()
    ]
    incidents += [
        {"kind": "issue", "at": at, **_point(None, None)}
        for (at,) in (
            await session.execute(select(OrderIssue.created_at).where(OrderIssue.order_id == oid))
        ).all()
    ]
    incidents.sort(key=lambda i: i["at"])
    return steps, [{**i, "at": i["at"].isoformat()} for i in incidents]


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
        steps, incidents = await _steps(session, oid)  # the steps without a map
        return 200, {"success": False, "reason": "no_location", "steps": steps, "incidents": incidents}
    customer = {"address": address, "lat": float(c_lat), "lng": float(c_lng)}

    vehicle = None
    offer = None
    position = None
    if courier_id is not None:
        vehicle = await session.scalar(select(Courier.vehicle).where(Courier.id == courier_id))
        accepted = (
            await session.execute(
                select(
                    OrderOffer.to_shop_km,
                    OrderOffer.eta_minutes,
                    OrderOffer.estimated_eta_minutes,
                    OrderOffer.courier_lat,
                    OrderOffer.courier_lng,
                    OrderOffer.courier_position_at,
                ).where(
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
            position = _position(accepted[3], accepted[4], accepted[5])
    other_offers = [
        {**found_at, "status": str(status)}
        for lat, lng, at, status, offer_courier in (
            await session.execute(
                select(
                    OrderOffer.courier_lat,
                    OrderOffer.courier_lng,
                    OrderOffer.courier_position_at,
                    OrderOffer.status,
                    OrderOffer.courier_id,
                )
                .where(OrderOffer.order_id == oid, OrderOffer.courier_lat.is_not(None))
                .order_by(OrderOffer.created_at)
            )
        ).all()
        if not (offer_courier == courier_id and str(status) == "accepted")
        and (found_at := _position(lat, lng, at)) is not None
    ]

    points = [(s["lat"], s["lng"]) for s in stops] + [(customer["lat"], customer["lng"])]
    if position is not None:
        points = [(position["lat"], position["lng"]), *points]
    found = await road_trip(points)
    source = "osrm"
    if found is None or len(found.legs) != len(points) - 1:
        found, source = straight_trip(points), "straight"
    legs = [_leg(leg, path, vehicle) for leg, path in zip(found.legs, found.leg_coords, strict=True)]
    to_shop = legs.pop(0) if position is not None else None
    course = found.legs[1:] if position is not None else found.legs
    steps, incidents = await _steps(session, oid)
    return 200, {
        "success": True,
        "source": source,
        "vehicle": str(vehicle) if vehicle else "scooter",
        "stops": stops,
        "customer": customer,
        "legs": legs,
        "distance_km": round(sum(leg.distance_m for leg in course) / 1000, 2),
        "drive_minutes": round(sum(leg_minutes(leg, vehicle) for leg in course)),
        "offer": offer,
        "courier_position": position,
        "to_shop": to_shop,
        "other_offers": other_offers,
        "steps": steps,
        "incidents": incidents,
    }
