"""Broadcast of a new (or re-opened) order to the couriers around its shop.

Port of base44/functions/dispatchOrderToCouriers:
1. the customer's own courier (invite link, confirmed by the customer's sign-up
   attribution) first, with "Votre client <prénom> a passé une commande", even
   outside his radius or governorate, when he is online, verified and free;
2. every other courier online, verified, free (no delivery in progress), whose
   last position is within his notification radius (default 10 km) of the shop.
   Couriers of the shop's governorate when there are any online, else all online
   (the legacy filter + fallback). One PostGIS query over the `couriers_dispatch`
   partial index replaces Base44's per-courier reads (audit §3.4 N+1).
QA orders are never broadcast. The caller holds the order lock.
"""

import math
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Order, OrderStop, User
from app.services import order_texts
from app.services.notifications import notify
from app.services.order_transitions import now_utc
from app.services.orders import dropped_by, first_stop, is_test_order, signup_attribution, stop_coordinates

BUSY_STATUSES = ("accepted", "at_shop", "purchased", "on_the_way")
MAX_FAN_OUT = 200
DEFAULT_RADIUS_KM = 10


def _first_name(full_name: str | None) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


def _busy(courier_id: Any) -> Any:
    return exists().where(Order.courier_id == courier_id, Order.status.in_(BUSY_STATUSES))


def _metadata(order: Order, stop: OrderStop | None, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "shop_name": stop.name if stop else None,
        "shop_address": stop.address if stop else None,
        "delivery_governorate": order.delivery_governorate,
        "delivery_address": order.delivery_address,
        "quick_actions": ["accept", "decline"],
        "recipient_role": "courier",
        "timestamp": now_utc().isoformat().replace("+00:00", "Z"),
        **extra,
    }


async def _push_wanted(session: AsyncSession, user_id: uuid.UUID) -> bool:
    row = (
        await session.execute(select(User.push_enabled, User.notify_new_orders).where(User.id == user_id))
    ).first()
    return bool(row and row[0] and row[1])


async def _preferred(
    session: AsyncSession, order: Order, stop: OrderStop | None, shop: tuple[float, float] | None
) -> tuple[Courier | None, str | None, float | None]:
    """(courier, skip reason, distance km) for the invite-link courier."""
    customer = await session.get(User, order.customer_id)
    courier = await session.get(Courier, order.preferred_courier_id)
    if customer is None or courier is None or signup_attribution(customer) != courier.id:
        return None, "not_attributed", None
    if courier.user_id == order.customer_id:
        return None, "self", None
    if not courier.is_online:
        return None, "offline", None
    if courier.verification != "verified":
        return None, "not_verified", None
    if (await session.execute(select(_busy(courier.id)))).scalar():
        return None, "busy", None
    distance = None
    if shop is not None and courier.last_location is not None:
        distance = (
            await session.execute(
                select(
                    func.ST_Distance(
                        Courier.last_location, cast(func.ST_MakePoint(shop[1], shop[0]), Geography(srid=4326))
                    )
                ).where(Courier.id == courier.id)
            )
        ).scalar()
        distance = float(distance) / 1000 if distance is not None else None
    return courier, None, distance


async def dispatch_order(session: AsyncSession, order: Order) -> dict[str, Any]:
    """Claims the run (last_dispatched_at) and notifies the couriers. Returns the Deno answer."""
    order.last_dispatched_at = now_utc()
    await session.flush()
    if is_test_order(order.items_text):
        return {"success": True, "dispatched": 0, "reason": "test_order"}

    stop = await first_stop(session, order.id)
    shop = await stop_coordinates(session, stop)
    items = (order.items_text or "")[:60]
    dispatched = 0
    skipped: list[dict[str, Any]] = []
    preferred_id: uuid.UUID | None = None
    preferred_notified = False
    dropped = await dropped_by(session, order.id)  # couriers who gave it up: never offered again

    if order.preferred_courier_id is not None:
        courier, reason, distance = await _preferred(session, order, stop, shop)
        if courier is not None and courier.user_id in dropped:
            courier, reason = None, "dropped"
        if courier is None:
            skipped.append({"id": str(order.preferred_courier_id), "reason": f"preferred_{reason}"})
        else:
            preferred_id = courier.id
            text = order_texts.new_order_preferred(
                _first_name(order.contact_name), items, stop.name if stop else None, distance
            )
            extra: dict[str, Any] = {"preferred_courier": True}
            if distance is not None:
                extra["distance_km"] = round(distance, 1)
            await notify(
                session,
                user_id=courier.user_id,
                type_="new_order",
                order_id=order.id,
                metadata=_metadata(order, stop, extra),
                **text,
            )
            if await _push_wanted(session, courier.user_id):
                preferred_notified = True
                dispatched += 1
            else:
                skipped.append({"id": str(courier.id), "reason": "pref_disabled"})

    if shop is None:
        return {
            "success": True,
            "dispatched": dispatched,
            "reason": "shop_coords_missing",
            "preferred_notified": preferred_notified,
        }

    shop_point = cast(func.ST_MakePoint(shop[1], shop[0]), Geography(srid=4326))
    online = [Courier.is_online.is_(True)]
    governorate = stop.governorate if stop else None
    if governorate:
        in_governorate = (
            await session.execute(
                select(func.count())
                .select_from(Courier)
                .where(*online, Courier.service_governorate == governorate)
            )
        ).scalar_one()
        if in_governorate:
            online.append(Courier.service_governorate == governorate)
    candidates_total = (
        await session.execute(select(func.count()).select_from(Courier).where(*online))
    ).scalar_one()

    radius_m = func.coalesce(func.nullif(Courier.notification_radius_km, 0), DEFAULT_RADIUS_KM) * 1000
    distance_m = func.ST_Distance(Courier.last_location, shop_point)
    conditions = [
        *online,
        Courier.verification == "verified",
        Courier.last_location.is_not(None),
        func.ST_DWithin(Courier.last_location, shop_point, radius_m),
        ~_busy(Courier.id),
        # a dual account is never offered his own order (createOrderOffer refuses it anyway)
        Courier.user_id != order.customer_id,
        User.deleted_at.is_(None),
    ]
    if preferred_id is not None:
        conditions.append(Courier.id != preferred_id)
    if dropped:
        conditions.append(Courier.user_id.not_in(dropped))
    eligible = (
        await session.execute(
            select(
                Courier.id,
                Courier.user_id,
                distance_m.label("distance_m"),
                User.push_enabled,
                User.notify_new_orders,
            )
            .join(User, User.id == Courier.user_id)
            .where(*conditions)
            .order_by(distance_m)
            .limit(MAX_FAN_OUT)
        )
    ).all()
    for courier_id, user_id, dist_m, push_enabled, wants_new_orders in eligible:
        distance = float(dist_m) / 1000
        await notify(
            session,
            user_id=user_id,
            type_="new_order",
            order_id=order.id,
            metadata=_metadata(order, stop, {"distance_km": round(distance, 1)}),
            **order_texts.new_order(items, stop.name if stop else None, distance),
        )
        if push_enabled and wants_new_orders:
            dispatched += 1
        else:
            skipped.append({"id": str(courier_id), "reason": "pref_disabled"})
    not_eligible = max(0, candidates_total - len(eligible) - (1 if preferred_id else 0))
    return {
        "success": True,
        "order_id": str(order.id),
        "dispatched": dispatched,
        "skipped": len(skipped) + not_eligible,
        "candidates_total": candidates_total,
        "preferred_notified": preferred_notified,
        "sample_skips": skipped[:5],
    }


def redispatch_wait_seconds(order: Order, now: datetime, minimum_seconds: int) -> int:
    """Seconds before a customer / admin may broadcast again (0 = now)."""
    if order.last_dispatched_at is None:
        return 0
    elapsed = (now - order.last_dispatched_at).total_seconds()
    return math.ceil(minimum_seconds - elapsed) if elapsed < minimum_seconds else 0
