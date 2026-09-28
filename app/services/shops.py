"""Shops: user proposals (proposeShop) and helpers shared by the Shop / ShopReview entities.

proposeShop rules (base44/functions/proposeShop): name 2-100, address 3-300, position in
Tunisia (29.5-38.5 N, 7-12.5 E), Tunisian phone (optional), one known category,
description ≤ 500, ≤ 30 menu items (name ≤ 100, price 0-10 000, https photos only),
at most MAX_PER_DAY proposals per user in 24 h (admins exempt), and the same name
within 150 m returns the existing shop instead of a duplicate. A proposal is
'pending': shown to its author only until an admin approves it (Shop entity).
"""

import math
import re
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.jsnum import js_number
from app.config import settings
from app.errors import ApiError
from app.models import Shop, ShopMenuItem
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.security.tokens import now_utc
from app.services import text_norm
from app.services.places import point

MAX_PER_DAY = 3
TWIN_RADIUS_M = 150
CATEGORIES = (
    "restaurant", "pharmacy", "supermarket", "bakery", "cafe", "grocery", "clothing", "electronics",
    "hardware", "other",
)  # fmt: skip
BOUNDS = {"min_lat": 29.5, "max_lat": 38.5, "min_lng": 7.0, "max_lng": 12.5}
_HTTPS_PHOTO = re.compile(r"^https://\S{1,500}$")
_TN_PREFIX = re.compile(r"^(00)?216(?=\d{8}$)")


def clean_text(value: Any, limit: int) -> str:
    """Deno `text()`: whitespace collapsed, trimmed, cut to `limit` characters."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


# --- file URLs --------------------------------------------------------------------------


def public_prefix() -> str:
    return f"{settings.public_files_base_url}/"


def key_from_public_url(url: Any) -> str | None:
    """The object key of one of OUR public uploads (`…/public/…`), else None."""
    if not isinstance(url, str):
        return None
    prefix = public_prefix()
    if not url.startswith(prefix):
        return None
    key = url[len(prefix) :]
    if not key.startswith("public/") or ".." in key or len(key) > 300:
        return None
    return key


def stored_photo(url: Any) -> str | None:
    """A menu photo: our upload → its key; another https URL (Base44 files) → kept as is."""
    key = key_from_public_url(url)
    if key:
        return key
    if isinstance(url, str) and _HTTPS_PHOTO.match(url):
        return url
    return None


def photo_url_of(stored: str | None) -> str | None:
    if not stored:
        return None
    return stored if stored.startswith(("https://", "http://")) else f"{public_prefix()}{stored}"


# --- proposeShop ----------------------------------------------------------------------------


def _phone(raw: Any) -> str | None:
    digits = _TN_PREFIX.sub("", re.sub(r"\D", "", str(raw)))
    if not re.fullmatch(r"\d{8}", digits):
        raise ApiError(400, "invalid_phone")
    return f"+216{digits}"


def _menu(raw: Any) -> list[dict[str, Any]]:
    items = raw[:30] if isinstance(raw, list) else []
    menu = []
    for item in items:
        m = item if isinstance(item, dict) else {}
        name = clean_text(m.get("name"), 100)
        price_raw = js_number(m["price"]) if "price" in m else math.nan  # Number(undefined) = NaN
        if not name or not math.isfinite(price_raw):
            continue
        price = math.floor(price_raw * 1000 + 0.5) / 1000
        if not 0 <= price <= 10000:
            continue
        entry: dict[str, Any] = {"name": name, "price": price}
        photo = stored_photo(m.get("photo_url")) if isinstance(m.get("photo_url"), str) else None
        if photo:
            entry["photo_key"] = photo
        if m.get("description"):
            entry["description"] = clean_text(m.get("description"), 200)
        menu.append(entry)
    return menu


def build_shop(body: Any) -> dict[str, Any]:
    """Validated proposal, or ApiError(400, <code>) — Deno `buildShop`."""
    b = body if isinstance(body, dict) else {}
    name = clean_text(b.get("name"), 100)
    if len(name) < 2:
        raise ApiError(400, "invalid_name")
    address = clean_text(b.get("address"), 300)
    if len(address) < 3:
        raise ApiError(400, "invalid_address")
    lat, lng = js_number(b.get("latitude")), js_number(b.get("longitude"))
    if not (
        math.isfinite(lat)
        and math.isfinite(lng)
        and BOUNDS["min_lat"] <= lat <= BOUNDS["max_lat"]
        and BOUNDS["min_lng"] <= lng <= BOUNDS["max_lng"]
    ):
        raise ApiError(400, "invalid_location")
    phone = _phone(b["phone"]) if b.get("phone") else None
    category = str(b["category"]) if b.get("category") else ""
    if category and category not in CATEGORIES:
        raise ApiError(400, "invalid_category")
    return {
        "name": name,
        "address": address,
        "latitude": lat,
        "longitude": lng,
        "phone": phone,
        "opening_hours": clean_text(b.get("opening_hours"), 100) or None,
        "categories": [category] if category else [],
        "description": clean_text(b.get("description"), 500) or None,
        "menu_items": _menu(b.get("menu_items")),
    }


async def _proposals_last_day(session: AsyncSession, user_id: uuid.UUID) -> int:
    since = now_utc() - timedelta(hours=24)
    return (
        await session.execute(
            select(func.count())
            .select_from(Shop)
            .where(Shop.proposed_by == user_id, func.coalesce(Shop.proposed_at, Shop.created_at) >= since)
        )
    ).scalar_one()


async def _twin(session: AsyncSession, name: str, lat: float, lng: float) -> Shop | None:
    """Same (compacted) name within 150 m, not rejected."""
    wanted = text_norm.compact(name)
    nearby = (
        (
            await session.execute(
                select(Shop)
                .where(
                    func.ST_DWithin(Shop.location, point(lat, lng), TWIN_RADIUS_M, False),
                    Shop.review_status != "rejected",
                )
                .order_by(Shop.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return next((shop for shop in nearby if text_norm.compact(shop.name) == wanted), None)


async def _free_osm_id(session: AsyncSession) -> str:
    stamp = int(time.time() * 1000)
    candidate = f"custom_{stamp}"
    while (await session.execute(select(Shop.id).where(Shop.osm_id == candidate))).first() is not None:
        candidate = f"custom_{stamp}_{uuid.uuid4().hex[:6]}"
    return candidate


async def propose_shop(session: AsyncSession, user: CurrentUser, body: Any) -> dict[str, Any]:
    """proposeShop: {success, shop:{id, name, review_status}, duplicate?}."""
    built = build_shop(body)
    if not user.is_admin and await _proposals_last_day(session, user.id) >= MAX_PER_DAY:
        raise ApiError(429, "too_many_proposals", "Too many shop proposals today", max=MAX_PER_DAY)
    twin = await _twin(session, built["name"], built["latitude"], built["longitude"])
    if twin is not None:
        return {
            "success": True,
            "duplicate": True,
            "shop": {"id": str(twin.id), "name": twin.name, "review_status": twin.review_status},
        }
    shop = Shop(
        osm_id=await _free_osm_id(session),
        name=built["name"],
        address=built["address"],
        phone=built["phone"],
        opening_hours=built["opening_hours"],
        categories=built["categories"],
        description=built["description"],
        location=f"SRID=4326;POINT({built['longitude']} {built['latitude']})",
        review_status="pending",
        proposed_by=user.id,
        proposed_at=now_utc(),
    )
    session.add(shop)
    await session.flush()
    for position, item in enumerate(built["menu_items"]):
        session.add(
            ShopMenuItem(
                shop_id=shop.id,
                name=item["name"],
                price=item["price"],
                photo_key=item.get("photo_key"),
                description=item.get("description"),
                position=position,
            )
        )
    await session.flush()
    emit(session, "Shop", "create", shop.id)
    return {"success": True, "shop": {"id": str(shop.id), "name": shop.name, "review_status": "pending"}}
