"""Courier profile writes: updateMyCourierProfile, the admin verification, live position
(trackCourierLocation) and presence expiry.

Base44 references: base44/functions/updateMyCourierProfile, trackCourierLocation;
pages/AdminDashboard.jsx updateCourierVerification.
"""

import math
import re
import uuid
from datetime import time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Courier, File, NoResponseCase, Order, OrderStatusEvent, OrderTracking
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.geo import TRACKING_BOUNDS, point
from app.services.phones import InvalidPhone, to_e164

VEHICLES = ("walking", "scooter", "car")
SIZES = ("petit", "moyen", "grand")
PRESENCE_TIMEOUT = timedelta(minutes=15)
RUNNING_EXPIRE = timedelta(hours=48)  # same activity rule as expire_stale_orders
MAX_LIVE_ORDERS = 5


class ProfileRefused(Exception):
    def __init__(self, status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status, self.error, self.extra = status, error, extra

    def body(self) -> dict[str, Any]:
        return {"error": self.error, **self.extra}


# --- field checks (the Deno pick(): allowed + well-typed, the rest reported) -----------------------

Check = Any  # (value) -> (ok, clean)


def _str(limit: int) -> Check:
    return lambda v: (True, v.strip()[:limit]) if isinstance(v, str) else (False, None)


def _num(low: float, high: float) -> Check:
    def check(v: Any) -> tuple[bool, Any]:
        if isinstance(v, str) and v.strip():
            try:
                v = float(v)
            except ValueError:
                return False, None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (low <= v <= high):
            return False, None
        return True, v

    return check


def _one_of(values: tuple[str, ...]) -> Check:
    return lambda v: (v in values, v)


def _bool(v: Any) -> tuple[bool, Any]:
    return isinstance(v, bool), v


def _hhmm(v: Any) -> tuple[bool, Any]:
    if isinstance(v, str) and v.strip() == "":
        return True, None
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v.strip()):
        return False, None
    parts = [int(p) for p in v.strip().split(":")]
    if parts[0] > 23 or parts[1] > 59:
        return False, None
    return True, time(parts[0], parts[1])


def _country(v: Any) -> tuple[bool, Any]:
    if isinstance(v, str) and (v.strip() == "" or re.fullmatch(r"[A-Za-z]{2}", v.strip())):
        return True, v.strip().upper() or None
    return False, None


# Bounds are the table's CHECKs (the Deno function allowed wider ranges nobody uses).
UPDATABLE: dict[str, Check] = {
    "is_online": _bool,
    "current_lat": _num(-90, 90),
    "current_lng": _num(-180, 180),
    "price_per_km": _num(0, 50),
    "min_fee": _num(0, 200),
    "notification_radius_km": _num(0, 100),
    "vehicle_type": _one_of(VEHICLES),
    "max_package_size": _one_of(SIZES),
    "service_country": _country,
    "service_governorate": _str(120),
    "service_city": _str(120),
    "service_start_time": _hhmm,
    "service_end_time": _hhmm,
}
CREATE_ONLY: dict[str, Check] = {
    "full_name": _str(120),
    "phone": _str(40),
    "cin_passport": _str(40),
    "id_photo_uri": _str(512),
}
REQUIRED_AT_CREATE = ("full_name", "phone", "vehicle_type", "price_per_km")
COLUMN = {
    "is_online": "is_online",
    "price_per_km": "price_per_km",
    "min_fee": "min_fee",
    "notification_radius_km": "notification_radius_km",
    "vehicle_type": "vehicle",
    "max_package_size": "max_package",
    "service_country": "service_country",
    "service_governorate": "service_governorate",
    "service_city": "service_city",
    "service_start_time": "service_start",
    "service_end_time": "service_end",
    "full_name": "display_name",
    "cin_passport": "id_document_number",
}


def pick(fields: Any, allowed: dict[str, Check]) -> tuple[dict[str, Any], list[str], list[str]]:
    clean: dict[str, Any] = {}
    ignored: list[str] = []
    invalid: list[str] = []
    for key, raw in (fields if isinstance(fields, dict) else {}).items():
        check = allowed.get(key)
        if check is None:
            ignored.append(key)
            continue
        if raw is None:
            continue
        ok, value = check(raw)
        if ok:
            clean[key] = value
        else:
            invalid.append(key)
    return clean, ignored, invalid


async def _private_upload(session: AsyncSession, user: CurrentUser, key: str) -> File | None:
    """The caller's own private upload (UploadPrivateFile answers its key as file_uri)."""
    if not key.startswith("private/"):
        return None
    row = (await session.execute(select(File).where(File.key == key).with_for_update())).scalar_one_or_none()
    if row is None or row.owner_id != user.id or row.visibility != "private":
        return None
    return row


def _apply(courier: Courier, clean: dict[str, Any]) -> None:
    for key, value in clean.items():
        column = COLUMN.get(key)
        if column is None:
            continue
        if column in ("price_per_km", "min_fee", "notification_radius_km"):
            value = Decimal(str(value))
        setattr(courier, column, value)
    if "current_lat" in clean and "current_lng" in clean:
        courier.last_location = point(clean["current_lat"], clean["current_lng"])
        courier.last_seen_at = ot.now_utc()
    if clean.get("is_online") is True:
        courier.last_seen_at = ot.now_utc()


async def create_profile(
    session: AsyncSession, user: CurrentUser, fields: Any
) -> tuple[Courier, list[str], bool]:
    """Onboarding (action 'create'). Returns (courier, ignored, existed). Idempotent."""
    existing = (
        await session.execute(select(Courier).where(Courier.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if existing is not None:
        return existing, [], True
    clean, ignored, invalid = pick(fields, {**UPDATABLE, **CREATE_ONLY})
    phone = None
    if "phone" in clean:
        try:
            phone = to_e164(clean["phone"])
        except InvalidPhone:
            invalid.append("phone")
    photo: File | None = None
    if clean.get("id_photo_uri"):
        photo = await _private_upload(session, user, clean["id_photo_uri"])
        if photo is None:
            invalid.append("id_photo_uri")
    if invalid:
        raise ProfileRefused(400, "invalid_fields", fields=invalid)
    missing = [k for k in REQUIRED_AT_CREATE if clean.get(k) in (None, "")]
    if missing:
        raise ProfileRefused(400, "missing_fields", fields=missing)
    courier = Courier(
        user_id=user.id,
        display_name=clean["full_name"],
        phone_e164=phone,
        id_document_number=clean.get("cin_passport") or "",
        vehicle=clean["vehicle_type"],
        price_per_km=Decimal(str(clean["price_per_km"])),
        is_online=False,
        verification="pending",
    )
    _apply(courier, {k: v for k, v in clean.items() if k not in ("full_name", "cin_passport", "is_online")})
    courier.is_online = False
    if photo is not None:
        courier.id_document_key = photo.key
        # From now on only the admin function signs it (POST /api/files/signed-url refuses).
        photo.purpose = "courier_id"
    session.add(courier)
    await session.flush()
    emit(session, "CourierProfile", "create", courier.id)
    return courier, ignored, False


async def update_profile(session: AsyncSession, courier: Courier, fields: Any) -> list[str]:
    clean, ignored, invalid = pick(fields, UPDATABLE)
    if invalid:
        raise ProfileRefused(400, "invalid_fields", fields=invalid)
    if clean:
        _apply(courier, clean)
        await session.flush()
        emit(session, "CourierProfile", "update", courier.id)
    return ignored


async def set_verification(session: AsyncSession, admin: CurrentUser, courier: Courier, status: str) -> None:
    """Admin decision on a courier (CourierProfile.update verification_status).

    No notification here: AdminDashboard writes the account_verified / account_rejected notice
    itself (admin Notification.create, pushed by the messaging port)."""
    if status not in ("pending", "verified", "rejected"):
        raise ApiError(400, "validation_error", "verification_status: expected pending, verified or rejected")
    if courier.verification == status:
        return
    courier.verification = status
    courier.verified_at = ot.now_utc() if status != "pending" else None
    courier.verified_by = admin.id if status != "pending" else None
    if status != "rejected":
        courier.rejection_reason = None
    await session.flush()


# --- live position (trackCourierLocation) -------------------------------------------------------------


def valid_coordinates(lat: Any, lng: Any) -> str | None:
    """None when fine, else the reason (GEO_VALIDATION of the Deno function)."""
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng))
    if not numbers:
        return "Coordinates must be numbers"
    if not all(math.isfinite(v) for v in (lat, lng)):
        return "Coordinates must be finite numbers"
    min_lat, max_lat, min_lng, max_lng = TRACKING_BOUNDS
    if not min_lat <= lat <= max_lat:
        return f"Latitude {lat} outside valid range [{min_lat}, {max_lat}]"
    if not min_lng <= lng <= max_lng:
        return f"Longitude {lng} outside valid range [{min_lng}, {max_lng}]"
    # No "excessive precision" refusal (ported from the Deno function): real Android
    # WebViews report full doubles such as 35.825614699999995, which it rejected.
    # publish_position rounds to 6 decimals (about 10 cm) instead.
    return None


def last_activity_expr() -> Any:
    """Latest real activity of an order (SQL): its own writes, its status events, its no-response
    case. The live position lives in order_tracking, so it never counts (expireStaleOrders rule)."""
    events = select(func.max(OrderStatusEvent.created_at)).where(OrderStatusEvent.order_id == Order.id)
    cases = select(
        func.max(
            func.greatest(NoResponseCase.started_at, NoResponseCase.resolved_at, NoResponseCase.final_at)
        )
    ).where(NoResponseCase.order_id == Order.id)
    return func.greatest(
        Order.created_at,
        Order.updated_at,
        Order.cancelled_at,
        Order.last_dispatched_at,
        events.scalar_subquery(),
        cases.scalar_subquery(),
    )


async def publish_position(
    session: AsyncSession, courier: Courier, lat: float, lng: float
) -> list[uuid.UUID]:
    """The fix goes onto the courier's orders in progress (not onto abandoned ones) and his profile."""
    lat, lng = round(float(lat), 6), round(float(lng), 6)
    now = ot.now_utc()
    active = list(
        (
            await session.execute(
                select(Order.id)
                .where(
                    Order.courier_id == courier.id,
                    Order.status.in_(ot.LIVE_STATUSES),
                    last_activity_expr() >= now - RUNNING_EXPIRE,
                )
                .order_by(Order.updated_at.desc())
                .limit(MAX_LIVE_ORDERS)
                .with_for_update(of=Order)
            )
        ).scalars()
    )
    fix = point(lat, lng)
    for order_id in active:
        stmt = insert(OrderTracking).values(
            order_id=order_id, courier_id=courier.id, location=fix, recorded_at=now
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[OrderTracking.order_id],
                set_={"courier_id": courier.id, "location": fix, "recorded_at": now},
            )
        )
        emit(session, "Order", "update", order_id)
    courier.last_location = fix
    courier.last_seen_at = now
    await session.flush()
    return active


async def expire_presence(session: AsyncSession) -> int:
    """Online couriers without a heartbeat (position, online switch) for 15 minutes → offline."""
    cutoff = ot.now_utc() - PRESENCE_TIMEOUT
    ids = list(
        (
            await session.execute(
                update(Courier)
                .where(
                    Courier.is_online.is_(True),
                    or_(Courier.last_seen_at.is_(None), Courier.last_seen_at < cutoff),
                )
                .values(is_online=False)
                .returning(Courier.id)
            )
        ).scalars()
    )
    for courier_id in ids:
        emit(session, "CourierProfile", "update", courier_id)
    return len(ids)
