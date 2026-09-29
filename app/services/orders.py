"""Order helpers shared by the order functions, plus placeOrder.

Base44 references (base44/functions): placeOrder, getCustomerReliability, _shared/authz.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Courier, Order, OrderStop, User, UserAddress, customer_stats
from app.models.orders import UNAVAILABLE_POLICIES
from app.realtime.events import emit
from app.services import order_transitions as ot
from app.services.geo import ORDER_BOUNDS, as_float, haversine_km, lat_of, lng_of, point, within
from app.services.phones import InvalidPhone, customer_phone_usable, to_e164

TEST_ORDER_RE = re.compile(r"QA TEST|\bPW-", re.IGNORECASE)
# Same pattern in SQL (\m = start of a word, like JS \b before a letter).
TEST_ORDER_SQL = r"QA TEST|\mPW-"

MAX_OPEN_ORDERS = 5
SUSPENDED_AT = 5  # no-response incidents in INCIDENT_WINDOW_DAYS (customer_stats)
SIGNUP_WINDOW = timedelta(hours=24)
PACKAGE_SIZES = ("petit", "moyen", "grand")
MAX_STOPS = 5
MAX_QUANTITY = 100  # orders.quantity CHECK (Base44 took up to 999; nothing above 100 exists)


def unavailable_policy(value: Any) -> str:
    """NewOrder's "si un article est indisponible" choice; unknown or missing → the courier calls."""
    return value if isinstance(value, str) and value in UNAVAILABLE_POLICIES else "call_me"


def is_test_order(items_text: str | None) -> bool:
    return bool(TEST_ORDER_RE.search(items_text or ""))


def is_qa_account(email: str | None) -> bool:
    return (email or "").strip().lower() in {e.lower() for e in settings.QA_ACCOUNTS}


def round3(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


async def courier_of_user(session: AsyncSession, user_id: uuid.UUID, lock: bool = False) -> Courier | None:
    stmt = select(Courier).where(Courier.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def courier_user_id(session: AsyncSession, courier_id: uuid.UUID | None) -> uuid.UUID | None:
    if courier_id is None:
        return None
    return (
        await session.execute(select(Courier.user_id).where(Courier.id == courier_id))
    ).scalar_one_or_none()


async def is_assigned_courier(session: AsyncSession, order: Order, user_id: uuid.UUID) -> bool:
    return order.courier_id is not None and await courier_user_id(session, order.courier_id) == user_id


async def active_incidents(session: AsyncSession, customer_id: uuid.UUID) -> int:
    """No-response incidents of the last 180 days (the customer_stats view)."""
    count = (
        await session.execute(
            select(customer_stats.c.no_response_incidents).where(customer_stats.c.user_id == customer_id)
        )
    ).scalar_one_or_none()
    return int(count or 0)


async def mirror_incidents(session: AsyncSession, customer_id: uuid.UUID) -> int:
    """After a case counted / voided an incident: the count is derived (customer_stats), the
    suspension flag is mirrored like the Deno functions did on every profile of the customer
    (`is_blacklisted = incidents >= 5`), and the customer's UserProfile row changes."""
    await session.flush()
    count = await active_incidents(session, customer_id)
    user = await session.get(User, customer_id, with_for_update=True)
    if user is not None:
        user.is_blacklisted = count >= SUSPENDED_AT
        await session.flush()
        emit(session, "UserProfile", "update", user.id)
    return count


async def first_stop(session: AsyncSession, order_id: uuid.UUID) -> OrderStop | None:
    return (
        await session.execute(select(OrderStop).where(OrderStop.order_id == order_id, OrderStop.seq == 0))
    ).scalar_one_or_none()


async def stop_coordinates(session: AsyncSession, stop: OrderStop | None) -> tuple[float, float] | None:
    if stop is None or stop.location is None:
        return None
    row = (
        await session.execute(
            select(lat_of(OrderStop.location), lng_of(OrderStop.location)).where(OrderStop.id == stop.id)
        )
    ).first()
    return (float(row[0]), float(row[1])) if row and row[0] is not None else None


# --- reliability (getCustomerReliability, src/lib/noResponsePolicy.js) -----------------------------

LEVEL_THRESHOLDS = {"warning": 2, "limited": 3, "suspended": 5}
LIMITED_MAX_ADVANCE_TND = 30
INCIDENT_WINDOW_DAYS = 180


def reliability_from_count(count: int) -> dict[str, Any]:
    incidents = max(0, int(count or 0))
    level = "ok"
    if incidents >= LEVEL_THRESHOLDS["suspended"]:
        level = "suspended"
    elif incidents >= LEVEL_THRESHOLDS["limited"]:
        level = "limited"
    elif incidents >= LEVEL_THRESHOLDS["warning"]:
        level = "warning"
    elif incidents >= 1:
        level = "notice"
    limited = level in ("limited", "suspended")
    return {
        "incidents": incidents,
        "level": level,
        "visible_to_couriers": incidents >= LEVEL_THRESHOLDS["warning"],
        "max_advance_tnd": LIMITED_MAX_ADVANCE_TND if limited else None,
        "phone_confirmation_required": limited,
        "suspended": level == "suspended",
        "window_days": INCIDENT_WINDOW_DAYS,
    }


# --- placeOrder -------------------------------------------------------------------------------------


class OrderRefused(Exception):
    def __init__(self, http_status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status = http_status
        self.error = error
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.error, **self.extra}


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def tunisian_phone(raw: Any) -> str | None:
    """'+216XXXXXXXX' from 8 digits with an optional 216 / 00216 prefix (placeOrder rule)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("216") and len(digits) == 11:
        local = digits[3:]
    elif digits.startswith("00216") and len(digits) == 13:
        local = digits[5:]
    else:
        local = digits
    if not re.fullmatch(r"\d{8}", local):
        return None
    # users/orders store E.164 (CHECK ^\+[1-9]...): a local number starting with 0 can't be stored
    return f"+216{local}"


def order_contact_phone(user: User, raw: Any) -> str:
    """The phone couriers will call: the profile's number when it is Tunisian or a verified
    foreign one, else a Tunisian number typed in the form (placeOrder rule). A foreign number
    that was not confirmed by the WhatsApp code -> 400 phone_unverified; none -> phone_required.

    While WhatsApp is not configured the code can't be sent: the profile's foreign number is
    accepted as it is (owner's rule, 2026-09-29, hotfix 89577eb)."""
    profile = user.phone_e164
    if profile and customer_phone_usable(profile, user.phone_verified_at):
        return profile
    typed = tunisian_phone(raw)
    if typed:
        return typed
    if not settings.whatsapp_enabled:
        raise OrderRefused(400, "phone_required")
    if profile:  # a foreign number, not verified
        raise OrderRefused(400, "phone_unverified")
    try:
        typed_e164 = to_e164(str(raw)) if isinstance(raw, str) else None
    except InvalidPhone:
        typed_e164 = None
    raise OrderRefused(400, "phone_unverified" if typed_e164 else "phone_required")


def signup_attribution(user: User) -> uuid.UUID | None:
    """The invite-link courier recorded with the profile at sign-up (src/lib/referral.js)."""
    if user.referred_by_courier_id is None or user.referred_at is None:
        return None
    created = user.profile_created_at
    if created is not None and abs(user.referred_at - created) > SIGNUP_WINDOW:
        return None
    return user.referred_by_courier_id


@dataclass
class BuiltOrder:
    order: Order
    stops: list[OrderStop]


def _parse_scheduled(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    from app.compat.dates import parse_legacy_datetime

    try:
        return parse_legacy_datetime(value)
    except ValueError:
        return None


def build_order(
    raw: Any, user: User, address: UserAddress | None, default_point: tuple[float, float] | None
) -> BuiltOrder:
    """The whitelisted, bounded order (placeOrder.buildOrder). Raises OrderRefused(400)."""
    o = raw if isinstance(raw, dict) else {}
    items_text = _text(o.get("items_text"), 2000)
    if not items_text:
        raise OrderRefused(400, "invalid_items")
    shop_name = _text(o.get("shop_name"), 200)
    if not shop_name:
        raise OrderRefused(400, "invalid_shop")
    shop_lat, shop_lng = as_float(o.get("shop_lat")), as_float(o.get("shop_lng"))
    if not within(shop_lat, shop_lng, ORDER_BOUNDS):
        raise OrderRefused(400, "invalid_shop_location")
    delivery_address = _text(o.get("delivery_address"), 300)
    if not delivery_address:
        raise OrderRefused(400, "invalid_delivery_address")
    d_lat_raw = (
        o.get("delivery_lat") if o.get("delivery_lat") is not None else (default_point or (None, None))[0]
    )
    d_lng_raw = (
        o.get("delivery_lng") if o.get("delivery_lng") is not None else (default_point or (None, None))[1]
    )
    delivery_lat, delivery_lng = as_float(d_lat_raw), as_float(d_lng_raw)
    if not within(delivery_lat, delivery_lng, ORDER_BOUNDS):
        raise OrderRefused(400, "invalid_delivery_location")
    phone = order_contact_phone(user, o.get("customer_phone"))
    assert shop_lat is not None and shop_lng is not None
    assert delivery_lat is not None and delivery_lng is not None

    quantity_raw = as_float(o.get("quantity"))
    quantity = round(quantity_raw) if quantity_raw is not None else None
    estimated = as_float(o.get("estimated_price"))
    scheduled = _parse_scheduled(o.get("scheduled_time")) if o.get("preferred_time") == "scheduled" else None
    package = o.get("package_size") if o.get("package_size") in PACKAGE_SIZES else "petit"

    order = Order(
        id=uuid.uuid4(),
        customer_id=user.id,
        contact_name=_text(user.full_name, 120),
        contact_phone_e164=phone,
        items_text=items_text,
        quantity=quantity if quantity is not None and 1 <= quantity <= MAX_QUANTITY else 1,
        notes=_text(o.get("notes"), 500),
        alternatives=_text(o.get("alternatives"), 500),
        unavailable_policy=unavailable_policy(o.get("unavailable_policy")),
        estimated_price=round3(estimated) if estimated is not None and 0 <= estimated <= 100000 else None,
        package=package,
        delivery_address=delivery_address,
        delivery_governorate=_text(o.get("delivery_governorate"), 80) or None,
        delivery_city=_text(o.get("delivery_city"), 80) or None,
        delivery_details=_text(o.get("delivery_details"), 300),
        delivery_location=point(delivery_lat, delivery_lng),
        scheduled_for=scheduled,
        distance_km=Decimal(str(round(haversine_km(shop_lat, shop_lng, delivery_lat, delivery_lng), 2))),
        payment_method="cash",
        preferred_courier_id=signup_attribution(user),
        current_stop_seq=0,
    )
    raw_shops = o.get("shops") if isinstance(o.get("shops"), list) else []
    first_items = raw_shops[0].get("items") if raw_shops and isinstance(raw_shops[0], dict) else None
    stops = [
        OrderStop(
            order_id=order.id,
            seq=0,
            name=shop_name,
            address=_text(o.get("shop_address"), 300),
            phone=_text(o.get("shop_phone"), 30),
            governorate=_text(o.get("shop_governorate"), 80) or None,
            city=_text(o.get("shop_city"), 80) or None,
            location=point(shop_lat, shop_lng),
            items=_text(first_items, 1000) or None,
            status="pending",
        )
    ]
    # shops[0] is the main shop (OrderForm sends it again); the others become the next stops.
    for index, shop in enumerate(raw_shops[1:MAX_STOPS], start=1):
        s = shop if isinstance(shop, dict) else {}
        lat, lng = as_float(s.get("lat")), as_float(s.get("lng"))
        stops.append(
            OrderStop(
                order_id=order.id,
                seq=index,
                name=_text(s.get("name"), 200) or f"#{index + 1}",
                address=_text(s.get("address"), 300),
                location=point(lat, lng)
                if within(lat, lng, ORDER_BOUNDS) and lat is not None and lng is not None
                else None,
                items=_text(s.get("items"), 1000) or None,
                status="pending",
            )
        )
    return BuiltOrder(order=order, stops=stops)


async def default_address(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[UserAddress | None, tuple[float, float] | None]:
    row = (
        await session.execute(
            select(UserAddress, lat_of(UserAddress.location), lng_of(UserAddress.location)).where(
                UserAddress.user_id == user_id, UserAddress.is_default
            )
        )
    ).first()
    if row is None:
        return None, None
    address, lat, lng = row
    return address, ((float(lat), float(lng)) if lat is not None else None)


async def place_order(session: AsyncSession, user_id: uuid.UUID, raw: Any) -> Order:
    """Validates and creates the order (placeOrder). Raises OrderRefused."""
    # The user row lock serializes a customer's concurrent placements (open-order cap).
    user = (await session.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    address, default_point = await default_address(session, user.id)
    built = build_order(raw, user, address, default_point)
    if await active_incidents(session, user.id) >= SUSPENDED_AT:
        raise OrderRefused(403, "customer_suspended")
    open_count = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(Order.customer_id == user.id, Order.status.in_(ot.OPEN_STATUSES))
        )
    ).scalar_one()
    if open_count >= MAX_OPEN_ORDERS:
        raise OrderRefused(429, "too_many_open_orders", max=MAX_OPEN_ORDERS)
    if built.order.preferred_courier_id is not None:
        exists = await session.get(Courier, built.order.preferred_courier_id)
        if exists is None:
            built.order.preferred_courier_id = None
    session.add(built.order)
    await session.flush()
    session.add_all(built.stops)
    await ot.start(session, built.order, user.id, "placeOrder")
    return built.order
