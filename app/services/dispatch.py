"""Broadcast of a new (or re-opened) order to the couriers around its shop.

dispatchOrderToCouriers:
1. the customer's own courier (invite link, confirmed by the customer's sign-up
   attribution) first, with "Votre client <prénom> a passé une commande", even
   outside his radius or governorate, when he is online, verified and free;
2. every other courier online, verified, free (no delivery in progress), whose
   last position is within his notification radius (default 10 km) of the shop.
   Couriers of the shop's governorate when there are any online, else all online
   (the legacy filter + fallback). One PostGIS query over the `couriers_dispatch`
   partial index, no per-courier reads (audit §3.4 N+1).
Launch mode (app/services/launch_mode.py, owner 13/10/2026, on by default): « online » is not
required — a courier « Hors ligne » or signed out is notified too (his last known position within
his radius, or no position yet but serving the shop's governorate). Switched off in Admin →
Paramètres once there are enough couriers.
QA orders (« QA TEST » / « PW- » in the items, settings.QA_ACCOUNTS) reach the QA couriers only:
same path, same notifications, nobody else (owner, 16/10/2026 — the ODS staging tests use it).
Invite-link head start (owner 18/10/2026, app/services/invite_priority.py): when the customer's own
courier can take the order (step 1) and it never had one, he ALONE is told and the others hear about
it 60 s later (the job invite_priority_release calls dispatch_order(after_head_start=True)).
The caller holds the order lock.
"""

import math
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Courier, Order, OrderStop, User
from app.services import invite_priority, launch_mode, order_texts, push_design
from app.services.notifications import notify
from app.services.order_transitions import now_utc
from app.services.orders import (
    dropped_by,
    first_stop,
    is_qa_account,
    is_test_order,
    signup_attribution,
    stop_coordinates,
)
from app.services.safety import blocked_with

BUSY_STATUSES = ("accepted", "at_shop", "purchased", "on_the_way")
MAX_FAN_OUT = 200
DEFAULT_RADIUS_KM = 10


def _first_name(full_name: str | None) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


def _busy(courier_id: Any) -> Any:
    return exists().where(Order.courier_id == courier_id, Order.status.in_(BUSY_STATUSES))


def _partner_extra(order: Order, stop: OrderStop | None) -> dict[str, Any]:
    """A restaurant ODS order (Lot 2): the label, the price known in advance and the owner's reminder
    (16/10/2026, guard-rail 2) travel with the notification."""
    if order.source != "partner":
        return {}
    from app.services.partner_orders import COURIER_NOTE_AR, COURIER_NOTE_FR

    return {
        "source": "partner",
        "partner_name": stop.name if stop else None,
        "items_total": float(order.estimated_price) if order.estimated_price is not None else None,
        "courier_note_fr": COURIER_NOTE_FR,
        "courier_note_ar": COURIER_NOTE_AR,
    }


def _metadata(order: Order, stop: OrderStop | None, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        **_partner_extra(order, stop),
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
    session: AsyncSession,
    order: Order,
    stop: OrderStop | None,
    shop: tuple[float, float] | None,
    offline_too: bool = False,
    qa_only: bool = False,
) -> tuple[Courier | None, str | None, float | None]:
    """(courier, skip reason, distance km) for the invite-link courier."""
    customer = await session.get(User, order.customer_id)
    courier = await session.get(Courier, order.preferred_courier_id)
    if customer is None or courier is None or signup_attribution(customer) != courier.id:
        return None, "not_attributed", None
    if courier.user_id == order.customer_id:
        return None, "self", None
    courier_user = await session.get(User, courier.user_id)
    if courier_user is None or courier_user.deleted_at is not None or courier_user.disabled_at is not None:
        return None, "inactive", None  # a deleted or disabled account: the others at once
    if qa_only and not is_qa_account(courier_user.email):
        return None, "not_qa", None
    if not courier.is_online and not offline_too:
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


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def dispatch_order(
    session: AsyncSession, order: Order, *, after_head_start: bool = False
) -> dict[str, Any]:
    """Claims the run (last_dispatched_at) and notifies the couriers. Returns the function's answer.
    after_head_start: the invite-link courier's 60 s just ended (invite_priority.release): he knows
    already, the others are told now."""
    now = now_utc()
    if invite_priority.running(order, now):
        # his 60 s are running: nobody else is told before they end (a re-broadcast waits too)
        return {
            "success": True,
            "order_id": str(order.id),
            "dispatched": 0,
            "reason": "invite_priority",
            "priority_until": _iso(order.priority_until),
            "preferred_notified": False,
        }
    if order.priority_until is not None and order.priority_released_at is None:
        order.priority_released_at = now  # ended, the job has not run yet: this broadcast is the one
    order.last_dispatched_at = now
    await session.flush()
    # a QA order (« QA TEST » / « PW- ») is offered to the QA accounts only (settings.QA_ACCOUNTS)
    qa_only = is_test_order(order.items_text)
    qa_emails = [e.lower() for e in settings.QA_ACCOUNTS]
    if qa_only and not qa_emails:
        return {"success": True, "dispatched": 0, "reason": "test_order", "qa_only": True}

    stop = await first_stop(session, order.id)
    shop = await stop_coordinates(session, stop)
    # the articles in words (QA 22/10: never « QA TEST [Recettes] Ojja… · Couscous au » cut mid-word)
    items = order_texts.notice_items(order.items_text)
    # what every courier's lock screen shows of the order (distance aside): built once
    card = await push_design.load_card(session, order.id)
    dispatched = 0
    skipped: list[dict[str, Any]] = []
    preferred_id: uuid.UUID | None = None
    preferred_notified = False
    dropped = await dropped_by(session, order.id)  # couriers who gave it up: never offered again
    dropped |= await blocked_with(session, order.customer_id)  # a block either way: never offered
    offline_too = await launch_mode.notify_offline_couriers(session)

    if after_head_start:
        preferred_id = order.priority_courier_id  # told 60 s ago: not twice
    elif order.preferred_courier_id is not None:
        courier, reason, distance = await _preferred(session, order, stop, shop, offline_too, qa_only)
        if courier is not None and courier.user_id in dropped:
            courier, reason = None, "dropped"
        if courier is None:
            skipped.append({"id": str(order.preferred_courier_id), "reason": f"preferred_{reason}"})
        else:
            preferred_id = courier.id
            wants_push = await _push_wanted(session, courier.user_id)
            # the head start: once per order, and only to a courier who will hear about it
            head_start = order.priority_until is None and wants_push
            until = invite_priority.start(session, order, courier.id, now) if head_start else None
            text = order_texts.new_order_preferred(
                _first_name(order.contact_name),
                items,
                stop.name if stop else None,
                distance,
                head_start_s=int(invite_priority.HEAD_START.total_seconds()) if head_start else None,
            )
            extra: dict[str, Any] = {"preferred_courier": True}
            if until is not None:
                extra["priority_until"] = _iso(until)
            if distance is not None:
                extra["distance_km"] = round(distance, 1)
            await notify(
                session,
                user_id=courier.user_id,
                type_="new_order",
                order_id=order.id,
                metadata=_metadata(order, stop, extra),
                push_card=card,
                **text,
            )
            if wants_push:
                preferred_notified = True
                dispatched += 1
            else:
                skipped.append({"id": str(courier.id), "reason": "pref_disabled"})
            if until is not None:
                await session.flush()
                return {
                    "success": True,
                    "order_id": str(order.id),
                    "dispatched": dispatched,
                    "qa_only": qa_only,
                    "reason": "invite_priority",
                    "priority_until": _iso(until),
                    "preferred_notified": preferred_notified,
                }

    if shop is None:
        return {
            "success": True,
            "dispatched": dispatched,
            "reason": "shop_coords_missing",
            "preferred_notified": preferred_notified,
        }

    shop_point = cast(func.ST_MakePoint(shop[1], shop[0]), Geography(srid=4326))
    online = [] if offline_too else [Courier.is_online.is_(True)]
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
    if offline_too:
        candidates_total = (
            await session.execute(
                select(func.count()).select_from(Courier).where(*online, Courier.verification == "verified")
            )
        ).scalar_one()

    radius_m = func.coalesce(func.nullif(Courier.notification_radius_km, 0), DEFAULT_RADIUS_KM) * 1000
    distance_m = func.ST_Distance(Courier.last_location, shop_point)
    near = func.ST_DWithin(Courier.last_location, shop_point, radius_m)
    if offline_too and governorate:
        # launch mode: a courier who never shared a position yet, but serves this governorate
        no_position_here = and_(Courier.last_location.is_(None), Courier.service_governorate == governorate)
        where_near = or_(near, no_position_here)
    else:
        where_near = and_(Courier.last_location.is_not(None), near)
    conditions = [
        *online,
        Courier.verification == "verified",
        where_near,
        ~_busy(Courier.id),
        # a dual account is never offered his own order (createOrderOffer refuses it anyway)
        Courier.user_id != order.customer_id,
        User.deleted_at.is_(None),
    ]
    if preferred_id is not None:
        conditions.append(Courier.id != preferred_id)
    if dropped:
        conditions.append(Courier.user_id.not_in(dropped))
    if qa_only:
        conditions.append(func.lower(User.email).in_(qa_emails))
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
            .order_by(distance_m.asc().nulls_last())
            .limit(MAX_FAN_OUT)
        )
    ).all()
    for courier_id, user_id, dist_m, push_enabled, wants_new_orders in eligible:
        distance = float(dist_m) / 1000 if dist_m is not None else None
        await notify(
            session,
            user_id=user_id,
            type_="new_order",
            order_id=order.id,
            metadata=_metadata(order, stop, {} if distance is None else {"distance_km": round(distance, 1)}),
            push_card=card,
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
        "qa_only": qa_only,
        "skipped": len(skipped) + not_eligible,
        "candidates_total": candidates_total,
        "preferred_notified": preferred_notified,
        "after_head_start": after_head_start,
        "sample_skips": skipped[:5],
    }


def redispatch_wait_seconds(order: Order, now: datetime, minimum_seconds: int) -> int:
    """Seconds before a customer / admin may broadcast again (0 = now)."""
    if order.last_dispatched_at is None:
        return 0
    elapsed = (now - order.last_dispatched_at).total_seconds()
    return math.ceil(minimum_seconds - elapsed) if elapsed < minimum_seconds else 0
