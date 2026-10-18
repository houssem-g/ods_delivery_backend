"""Vitrine (étape 8, 16/10/2026): administration (admin only), visibility, hours, client reads.

Contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md (§2 visibility, §3 admin functions, §4 client
reads, §8 merchant link). The catalog snapshot pushed by ODS and the switch between the two catalog
sources live in app/services/vitrine_catalog.py; the order side in app/services/vitrine_orders.py;
the visits and the merchant's counters in app/services/vitrine_stats.py.

Visibility is enforced here, server side, never only hidden by the front: a shop WITH a Vitrine is
shown to admins always, to the QA accounts when the Vitrine is active, to everybody else only when
it is active, `shops.visibility = 'public'` AND the global switch « Vitrines publiques autorisées »
(`app_settings['vitrine'].public_allowed`, off by default) is on. Anything else answers exactly like
an unknown shop.
"""

import contextlib
import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, and_, exists, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import (
    AppSetting,
    Partner,
    PartnerCatalog,
    Shop,
    ShopMenuItem,
    Vitrine,
    VitrineCategory,
    VitrinePoster,
    VitrineProduct,
)
from app.models.vitrine import MAX_PRODUCT_PRICE
from app.security import secret_box
from app.security.deps import CurrentUser
from app.services import shops as shop_service
from app.services import text_norm, vitrine_options
from app.services.geo import ORDER_BOUNDS, within
from app.services.orders import is_qa_account
from app.services.places import lat_expr, lng_expr

SETTINGS_KEY = "vitrine"
PUBLIC_ALLOWED = "public_allowed"
STALE_AFTER = timedelta(hours=26)
RESTORE_WINDOW = timedelta(days=30)
TUNIS = ZoneInfo("Africa/Tunis")
STATUSES = ("draft", "active", "paused", "archived")
SOURCES = ("manual", "ods")
VISIBILITIES = ("qa", "public")
_HHMM = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
OSM_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")  # index = ISO day - 1
NAME_MAX = 100
CATEGORY_NAME_MAX = 60
DESCRIPTION_MAX = 300
NOTE_MAX = 120

shops = Shop.__table__
vit = Vitrine.__table__


def _admin(user: CurrentUser | None) -> None:
    if user is None or not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")


def invalid(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def money(value: Any) -> Decimal | None:
    """A price 0–2000 DT, 3 decimals; None when not a number in range."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > MAX_PRODUCT_PRICE:
        return None
    return amount


def num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def clean(value: Any, limit: int) -> str:
    return shop_service.clean_text(value, limit)


# --- settings (the global switch) -------------------------------------------------------------------


async def public_allowed(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return value.get(PUBLIC_ALLOWED) is True


def public_allowed_sql() -> Any:
    """The switch in SQL (for the read policies that cannot await)."""
    flag = (
        select(AppSetting.value[PUBLIC_ALLOWED].astext.cast(Boolean))
        .where(AppSetting.key == SETTINGS_KEY)
        .scalar_subquery()
    )
    return func.coalesce(flag, false())


async def get_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    return {"success": True, PUBLIC_ALLOWED: await public_allowed(session)}


async def set_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    wanted = payload.get(PUBLIC_ALLOWED)
    if not isinstance(wanted, bool):
        raise invalid(f"{PUBLIC_ALLOWED}: true or false")
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    value[PUBLIC_ALLOWED] = wanted
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=value, updated_by=admin.id))
    else:
        row.value, row.updated_by = value, admin.id
    await session.flush()
    return {"success": True, PUBLIC_ALLOWED: wanted}


# --- visibility -------------------------------------------------------------------------------------


def viewer_kind(viewer: CurrentUser | None) -> str:
    if viewer is None:
        return "anonymous"
    if viewer.is_admin:
        return "admin"
    if is_qa_account(viewer.email):
        return "qa"
    return "public"


def shop_visible_sql(viewer: CurrentUser | None, shop_id: Any = None, visibility: Any = None) -> Any:
    """The Vitrine layer of a shop read policy (ANDed with the existing review rules)."""
    shop_id = shops.c.id if shop_id is None else shop_id
    visibility = shops.c.visibility if visibility is None else visibility
    kind = viewer_kind(viewer)
    if kind == "admin":
        return true()
    v = vit.alias("visible_vitrine")  # its own alias: the outer query may join `vitrines` too
    has_vitrine = exists().where(v.c.shop_id == shop_id)
    active = exists().where(v.c.shop_id == shop_id, v.c.status == "active")
    if kind == "qa":
        return or_(~has_vitrine, active)
    return or_(
        and_(~has_vitrine, visibility == "public"),
        and_(active, visibility == "public", public_allowed_sql()),
    )


def effectively_public(shop: Shop, vitrine: Vitrine, allowed: bool) -> bool:
    return shop.visibility == "public" and allowed and not vitrine.catalog_test


def can_see(kind: str, shop: Shop, vitrine: Vitrine, allowed: bool, *, as_if_active: bool = False) -> bool:
    if kind == "admin":
        return True
    if vitrine.status != "active" and not as_if_active:
        return False
    if kind == "qa":
        return True
    return shop.visibility == "public" and allowed


async def find_visible(
    session: AsyncSession, viewer: CurrentUser | None, *, shop_id: Any = None, slug: Any = None
) -> tuple[Shop, Vitrine]:
    """The Vitrine the viewer may see, else ApiError 404 not_found (identical to an unknown shop) or
    404 vitrine_unavailable (paused / archived, only to someone who could see it active)."""
    stmt = select(Shop, Vitrine).join(Vitrine, Vitrine.shop_id == Shop.id)
    sid = as_uuid(shop_id) if shop_id not in (None, "") else None
    if sid is not None:
        stmt = stmt.where(Shop.id == sid)
    elif isinstance(slug, str) and slug.strip():
        stmt = stmt.where(Shop.slug == slug.strip().lower()[:80])
    else:
        raise ApiError(404, "not_found", "Magasin introuvable")
    row = (await session.execute(stmt)).first()
    if row is None:
        raise ApiError(404, "not_found", "Magasin introuvable")
    shop, vitrine = row
    kind = viewer_kind(viewer)
    allowed = await public_allowed(session)
    if can_see(kind, shop, vitrine, allowed):
        return shop, vitrine
    if vitrine.status in ("paused", "archived") and can_see(kind, shop, vitrine, allowed, as_if_active=True):
        raise ApiError(404, "vitrine_unavailable", "Ce magasin n'est plus disponible")
    raise ApiError(404, "not_found", "Magasin introuvable")


# --- hours ------------------------------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def clean_hours(raw: Any) -> dict[str, Any] | None:
    """{tz, weekly:[{days:[1..7], open, close}]} validated; None = unknown. ApiError 400 otherwise."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("weekly"), list):
        raise invalid("hours: {tz, weekly: [{days, open, close}]}")
    weekly = []
    for rule in raw["weekly"][:21]:
        if not isinstance(rule, dict):
            raise invalid("hours.weekly: objects")
        days = rule.get("days")
        if (
            not isinstance(days, list)
            or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days)
        ):
            raise invalid("hours.weekly[].days: 1 (Monday) to 7 (Sunday)")
        opens, closes = rule.get("open"), rule.get("close")
        if not isinstance(opens, str) or not _HHMM.match(opens) or not isinstance(closes, str):
            raise invalid("hours.weekly[].open / close: HH:MM")
        if not _HHMM.match(closes) or _minutes(opens) > 1440 or _minutes(closes) > 1440:
            raise invalid("hours.weekly[].open / close: HH:MM")
        weekly.append({"days": sorted(set(days)), "open": opens, "close": closes})
    if len(raw["weekly"]) > 21:
        raise invalid("hours.weekly: at most 21 ranges")
    return {"tz": "Africa/Tunis", "weekly": weekly}


def _ranges_by_day(hours: dict[str, Any] | None) -> dict[int, list[tuple[int, int]]]:
    """ISO day → [(from, to)] in minutes (to > 1440 past midnight)."""
    out: dict[int, list[tuple[int, int]]] = {d: [] for d in range(1, 8)}
    for rule in (hours or {}).get("weekly") or []:
        start, end = _minutes(rule["open"]), _minutes(rule["close"])
        if end <= start:
            end += 1440
        for day in rule["days"]:
            out[day].append((start, end))
    for day in out:
        out[day].sort()
    return out


def osm_opening_hours(hours: dict[str, Any] | None) -> str | None:
    """The OSM text the front's openingHours.js reads: `Mo-Fr 08:00-12:00,14:00-19:00; Sa 09:00-13:00`."""
    if not hours or not hours.get("weekly"):
        return None
    by_day = _ranges_by_day(hours)

    def fmt(minutes: int) -> str:
        minutes %= 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def label(ranges: list[tuple[int, int]]) -> str:
        return ",".join(f"{fmt(a)}-{'24:00' if b == 1440 else fmt(b)}" for a, b in ranges)

    groups: list[tuple[list[int], str]] = []
    for day in range(1, 8):
        if not by_day[day]:
            continue
        text = label(by_day[day])
        if groups and groups[-1][1] == text and groups[-1][0][-1] == day - 1:
            groups[-1][0].append(day)
        else:
            groups.append(([day], text))
    parts = []
    for days, text in groups:
        span = (
            OSM_DAYS[days[0] - 1] if len(days) == 1 else f"{OSM_DAYS[days[0] - 1]}-{OSM_DAYS[days[-1] - 1]}"
        )
        parts.append(f"{span} {text}")
    return "; ".join(parts)[:300] or None


def _week_stretches(by_day: dict[int, list[tuple[int, int]]]) -> list[tuple[int, int]]:
    """The week's open ranges in minutes from Monday 00:00, touching / overlapping ranges merged, the
    Sunday → Monday wrap included (a stretch may end after 7 × 1440; one of 7 × 1440 = always open)."""
    week = 7 * 1440
    ranges = sorted(
        (base + s, base + e) for d in range(1, 8) for s, e in by_day[d] for base in [(d - 1) * 1440]
    )
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if len(merged) > 1 and merged[-1][1] >= week + merged[0][0]:  # Sunday night runs into Monday
        last = merged.pop()
        merged[0] = [last[0], max(last[1], week + merged[0][1])]
    return [(a, min(b, a + week)) for a, b in merged]


def open_state(hours: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """{open_now, opens_at, closes_at} in Tunisia time; open_now None when the hours are unknown."""
    if not hours or not hours.get("weekly"):
        return {"open_now": None, "opens_at": None, "closes_at": None}
    local = (now or datetime.now(UTC)).astimezone(TUNIS)
    day, minute = local.isoweekday(), local.hour * 60 + local.minute
    by_day = _ranges_by_day(hours)

    def hhmm(minutes: int) -> str:
        minutes %= 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # Open now: the closing time is the end of the CONTINUOUS stretch (a « 00:00–24:00 » every day, or
    # 22:00–24:00 followed by 00:00–02:00, never closes at midnight); open all week → closes_at None.
    week = 7 * 1440
    now_w = (day - 1) * 1440 + minute
    for start, end in _week_stretches(by_day):
        if start <= now_w < end or start <= now_w + week < end:
            return {
                "open_now": True,
                "opens_at": None,
                "closes_at": None if end - start >= week else hhmm(end),
            }
    for offset in range(0, 8):
        d = (day - 1 + offset) % 7 + 1
        for start, _end in by_day[d]:
            if offset > 0 or start > minute:
                return {"open_now": False, "opens_at": hhmm(start), "closes_at": None}
    return {"open_now": False, "opens_at": None, "closes_at": None}


# --- slugs --------------------------------------------------------------------------------------------


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:60].strip("-")


async def free_slug(session: AsyncSession, base: str, shop_id: uuid.UUID | None = None) -> str:
    root = base if len(base) >= 3 else (f"{base}-vitrine".strip("-") if base else "vitrine")
    candidate, n = root, 1
    while True:
        taken = (
            await session.execute(select(Shop.id).where(Shop.slug == candidate, Shop.id != shop_id))
        ).first()
        if taken is None:
            return candidate
        n += 1
        candidate = f"{root[:60]}-{n}"


def _wanted_slug(value: Any) -> str:
    slug = slugify(str(value or ""))
    if not 3 <= len(slug) <= 80:
        raise invalid("slug: 3 to 80 characters a-z, 0-9, -")
    return slug


# --- serialisation ------------------------------------------------------------------------------------


async def _coords(session: AsyncSession, shop: Shop) -> tuple[float | None, float | None]:
    row = (
        await session.execute(
            select(lat_expr(Shop.location), lng_expr(Shop.location)).where(Shop.id == shop.id)
        )
    ).first()
    return (float(row[0]), float(row[1])) if row and row[0] is not None else (None, None)


def app_url(path: str) -> str:
    return f"{settings.PUBLIC_APP_URL.rstrip('/')}{path}"


def poster_url(shop: Shop, code: str) -> str:
    return app_url(f"/v/{shop.slug}?src={code}")


def catalog_stale(vitrine: Vitrine, checked_at: datetime | None, now: datetime | None = None) -> bool:
    if vitrine.source != "ods":
        return False
    last = max(filter(None, [vitrine.catalog_synced_at, checked_at]), default=None)
    return last is None or (now or datetime.now(UTC)) - last > STALE_AFTER


async def _partner_catalog(session: AsyncSession, partner_id: uuid.UUID | None) -> PartnerCatalog | None:
    return await session.get(PartnerCatalog, partner_id) if partner_id is not None else None


async def _active_rows(session: AsyncSession, model: Any, shop_id: uuid.UUID) -> list[Any]:
    return list(
        (
            await session.execute(
                select(model)
                .where(model.shop_id == shop_id, model.archived_at.is_(None))
                .order_by(model.position, model.created_at, model.id)
            )
        ).scalars()
    )


def category_json(category: VitrineCategory, *, admin: bool = False) -> dict[str, Any]:
    out = {"id": str(category.id), "name": category.name, "position": category.position}
    if admin:
        out["ods_category_id"] = category.ods_category_id
    return out


def product_json(
    product: VitrineProduct, *, admin: bool = False, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    out = {
        "id": str(product.id),
        "category_id": str(product.category_id) if product.category_id else None,
        "name": product.name,
        "description": product.description,
        "price": float(product.price),
        "photo_url": shop_service.photo_url_of(product.photo_key),
        "available": product.available,
        "position": product.position,
        "options": options if options is not None else {"remove": [], "extras": []},
    }
    if admin:
        out["ods_menu_id"] = product.ods_menu_id
    return out


def _identity_differs(shop: Shop, lat: float | None, lng: float | None, restaurant: dict[str, Any]) -> bool:
    from app.services.places import haversine_m

    if restaurant.get("name") and text_norm.compact(restaurant["name"]) != text_norm.compact(shop.name):
        return True
    if restaurant.get("address") and clean(restaurant["address"], 300) != (shop.address or ""):
        return True
    r_lat, r_lng = restaurant.get("lat"), restaurant.get("lng")
    return None not in (r_lat, r_lng, lat, lng) and haversine_m(lat, lng, r_lat, r_lng) > 50


async def checklist(session: AsyncSession, shop: Shop, vitrine: Vitrine) -> dict[str, bool]:
    lat, lng = await _coords(session, shop)
    available = (
        await session.execute(
            select(func.count())
            .select_from(VitrineProduct)
            .where(
                VitrineProduct.shop_id == shop.id,
                VitrineProduct.archived_at.is_(None),
                VitrineProduct.available.is_(True),
            )
        )
    ).scalar_one()
    return {
        "position": within(lat, lng, ORDER_BOUNDS),
        "available_product": available > 0,
        "hours": bool(vitrine.hours and vitrine.hours.get("weekly")),
        "phone": bool(shop.phone),
        "ods_not_test": not vitrine.catalog_test,
        "global_switch": await public_allowed(session),
    }


async def vitrine_json(session: AsyncSession, shop: Shop, vitrine: Vitrine) -> dict[str, Any]:
    """The admin object (contract §3). Never the merchant token."""
    lat, lng = await _coords(session, shop)
    partner = await session.get(Partner, vitrine.partner_id) if vitrine.partner_id else None
    snapshot = await _partner_catalog(session, vitrine.partner_id)
    counts = (
        await session.execute(
            select(
                select(func.count())
                .select_from(VitrineCategory)
                .where(VitrineCategory.shop_id == shop.id, VitrineCategory.archived_at.is_(None))
                .scalar_subquery(),
                select(func.count())
                .select_from(VitrineProduct)
                .where(VitrineProduct.shop_id == shop.id, VitrineProduct.archived_at.is_(None))
                .scalar_subquery(),
                select(func.count())
                .select_from(VitrineProduct)
                .where(
                    VitrineProduct.shop_id == shop.id,
                    VitrineProduct.archived_at.is_(None),
                    VitrineProduct.available.is_(True),
                )
                .scalar_subquery(),
            )
        )
    ).one()
    from app.services import vitrine_stats

    posters = []
    scans = await vitrine_stats.month_scans(session, shop.id)
    for poster in (
        await session.execute(
            select(VitrinePoster)
            .where(VitrinePoster.shop_id == shop.id, VitrinePoster.archived_at.is_(None))
            .order_by(VitrinePoster.created_at)
        )
    ).scalars():
        posters.append(
            {
                "code": poster.code,
                "label": poster.label,
                "url": poster_url(shop, poster.code),
                "scans_month": scans.get(poster.code, 0),
                "created_at": iso(poster.created_at),
            }
        )
    checks = await checklist(session, shop, vitrine)
    identity = None
    if vitrine.source == "ods" and snapshot is not None:
        restaurant = (snapshot.payload or {}).get("restaurant") or {}
        identity = {
            "differs": _identity_differs(shop, lat, lng, restaurant),
            "name": restaurant.get("name"),
            "address": restaurant.get("address"),
            "phone": restaurant.get("phone"),
            "lat": restaurant.get("lat"),
            "lng": restaurant.get("lng"),
        }
    state = open_state(vitrine.hours)
    return {
        "shop_id": str(shop.id),
        "slug": shop.slug,
        "name": shop.name,
        "address": shop.address,
        "city": shop.city,
        "governorate": shop.governorate,
        "phone": shop.phone,
        "lat": lat,
        "lng": lng,
        "category": shop.categories[0] if shop.categories else None,
        "photo_url": shop_service.photo_url_of(shop.photo_key),
        "description": shop.description,
        "opening_hours": shop.opening_hours,
        "status": vitrine.status,
        "visibility": shop.visibility,
        "source": vitrine.source,
        "partner": {
            "id": str(partner.id),
            "name": partner.name,
            "active": partner.active,
            "ods_restaurant_id": partner.ods_restaurant_id,
        }
        if partner is not None
        else None,
        "catalog": {
            "version": vitrine.catalog_version,
            "synced_at": iso(vitrine.catalog_synced_at),
            "checked_at": iso(snapshot.checked_at) if snapshot is not None else None,
            "test": vitrine.catalog_test,
            "stale": catalog_stale(vitrine, snapshot.checked_at if snapshot is not None else None),
        },
        "hours": vitrine.hours,
        "open_now": state["open_now"],
        "opens_at": state["opens_at"],
        "counts": {"categories": counts[0], "products": counts[1], "available_products": counts[2]},
        "merchant_link": merchant_link_json(vitrine),
        "posters": posters,
        "public_checklist": checks,
        "can_go_public": all(checks.values()),
        "ods_identity": identity,
        "created_at": iso(vitrine.created_at),
        "updated_at": iso(max(vitrine.updated_at, shop.updated_at)),
    }


def merchant_link_json(vitrine: Vitrine) -> dict[str, Any] | None:
    if not vitrine.merchant_token_hash:
        return None
    return {
        "number": vitrine.merchant_link_no,
        "opens": vitrine.merchant_link_opens,
        "created_at": iso(vitrine.merchant_link_created_at),
    }


# --- admin: loading ---------------------------------------------------------------------------------


async def load(session: AsyncSession, shop_id: Any, *, lock: bool = False) -> tuple[Shop, Vitrine]:
    sid = as_uuid(shop_id)
    if sid is None:
        raise ApiError(404, "vitrine_not_found", "Vitrine not found")
    stmt = select(Shop, Vitrine).join(Vitrine, Vitrine.shop_id == Shop.id).where(Shop.id == sid)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).first()
    if row is None:
        raise ApiError(404, "vitrine_not_found", "Vitrine not found")
    return row[0], row[1]


async def linkable_partner(session: AsyncSession, raw_id: Any, shop_id: uuid.UUID | None) -> Partner:
    pid = as_uuid(raw_id)
    partner = await session.get(Partner, pid) if pid is not None else None
    if partner is None:
        raise ApiError(404, "partner_not_found", "Partner not found")
    if not partner.active or partner.revoked_at is not None or partner.kind != "ods_restaurant":
        raise ApiError(409, "partner_inactive", "Only an active ODS restaurant partner")
    linked = (
        await session.execute(select(Vitrine.shop_id).where(Vitrine.partner_id == partner.id))
    ).scalar_one_or_none()
    if linked is not None and linked != shop_id:
        raise ApiError(
            409, "partner_already_linked", "This restaurant already has a Vitrine", shop_id=str(linked)
        )
    return partner


# --- admin functions ----------------------------------------------------------------------------------


async def list_vitrines(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    stmt = select(Shop, Vitrine).join(Vitrine, Vitrine.shop_id == Shop.id).order_by(Vitrine.created_at.desc())
    if not payload.get("include_archived"):
        stmt = stmt.where(Vitrine.status != "archived")
    rows = (await session.execute(stmt)).all()
    return {"success": True, "vitrines": [await vitrine_json(session, s, v) for s, v in rows]}


async def get_admin(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"))
    categories = await _active_rows(session, VitrineCategory, shop.id)
    products = await _active_rows(session, VitrineProduct, shop.id)
    options = await vitrine_options.shop_options(session, shop, products, categories)
    names = {c.id: c.name for c in categories}
    return {
        "success": True,
        "vitrine": await vitrine_json(session, shop, vitrine),
        "categories": [category_json(c, admin=True) for c in categories],
        "products": [
            admin_product_json(shop, p, names.get(p.category_id), options.get(p.id)) for p in products
        ],
    }


def admin_product_json(
    shop: Shop, product: VitrineProduct, category_name: str | None, options: dict[str, Any] | None
) -> dict[str, Any]:
    """The admin's product: + `options_auto` (the engine, uncapped, before corrections),
    `options_override`, `ingredients` (the ODS recipe)."""
    kind = shop.categories[0] if shop.categories else None
    out = product_json(product, admin=True, options=options)
    out["options_auto"] = vitrine_options.candidates(
        product.name, product.description, category_name, product.ods_ingredients, kind
    )
    out["options_override"] = product.options_override
    out["ingredients"] = product.ods_ingredients
    return out


async def set_product_options(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    """setVitrineProductOptions: hide, add or order a dish's removals (also on a catalog linked to
    ODS: the options are Delivery's own). `reset: true` = back to automatic."""
    _admin(admin)
    shop, _vitrine = await load(session, payload.get("shop_id"), lock=True)
    product = await _product(session, shop.id, payload.get("product_id"))
    category = await session.get(VitrineCategory, product.category_id) if product.category_id else None
    category_name = category.name if category is not None and category.archived_at is None else None
    kind = shop.categories[0] if shop.categories else None
    auto = vitrine_options.candidates(
        product.name, product.description, category_name, product.ods_ingredients, kind
    )
    product.options_override = vitrine_options.clean_override(payload, {o["key"] for o in auto})
    await session.flush()
    await session.refresh(product)
    return {"success": True, "product": await _admin_product(session, shop, product)}


def _new_shop_fields(raw: Any) -> dict[str, Any]:
    s = raw if isinstance(raw, dict) else {}
    try:
        built = shop_service.build_shop(
            {
                "name": s.get("name"),
                "address": s.get("address"),
                "latitude": s.get("lat"),
                "longitude": s.get("lng"),
                "phone": s.get("phone") or None,
                "category": s.get("category") or None,
                "description": s.get("description"),
            }
        )
    except ApiError as exc:
        raise invalid(f"shop: {exc.error}") from None
    built["city"] = clean(s.get("city"), 120) or None
    built["governorate"] = clean(s.get("governorate"), 120) or None
    built["photo_key"] = shop_service.stored_photo(s.get("photo_url")) if s.get("photo_url") else None
    return built


async def create_vitrine(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    from app.services import vitrine_catalog, vitrine_requests

    _admin(admin)
    # « Accepter » a restaurant's request: checked open first, accepted with the Vitrine (17/10)
    request_row = (
        await vitrine_requests.open_request(session, payload.get("request_id"))
        if payload.get("request_id") not in (None, "")
        else None
    )
    source = payload.get("source") or "manual"
    if source not in SOURCES:
        raise invalid("source: manual or ods")
    hours = clean_hours(payload.get("hours")) if "hours" in payload else None
    now = datetime.now(UTC)
    transformed = payload.get("shop_id") not in (None, "")
    if transformed:
        sid = as_uuid(payload.get("shop_id"))
        shop = await session.get(Shop, sid, with_for_update=True) if sid is not None else None
        if shop is None:
            raise ApiError(404, "shop_not_found", "Shop not found")
        if await session.get(Vitrine, shop.id) is not None:
            raise ApiError(409, "vitrine_exists", "This shop already has a Vitrine")
    else:
        built = _new_shop_fields(payload.get("shop"))
        twin = await shop_service._twin(session, built["name"], built["latitude"], built["longitude"])
        if twin is not None:
            raise ApiError(
                409,
                "shop_exists",
                "Same name within 150 m: transform it",
                shop_id=str(twin.id),
                name=twin.name,
            )
        shop = Shop(
            osm_id=await shop_service._free_osm_id(session),
            name=built["name"],
            address=built["address"],
            phone=built["phone"],
            city=built["city"],
            governorate=built["governorate"],
            categories=built["categories"],
            description=built["description"],
            photo_key=built["photo_key"],
            location=f"SRID=4326;POINT({built['longitude']} {built['latitude']})",
            review_status="approved",
            reviewed_by=admin.id,
            reviewed_at=now,
            visibility="qa",
        )
        session.add(shop)
        await session.flush()
    partner = await linkable_partner(session, payload.get("partner_id"), shop.id) if source == "ods" else None
    if shop.review_status != "approved":
        shop.review_status, shop.reviewed_by, shop.reviewed_at = "approved", admin.id, now
    shop.visibility = "qa"  # a new Vitrine is a test until an admin publishes it
    if payload.get("slug"):
        slug = _wanted_slug(payload["slug"])
        if (await session.execute(select(Shop.id).where(Shop.slug == slug, Shop.id != shop.id))).first():
            raise ApiError(409, "slug_taken", "This address is taken")
        shop.slug = slug
    elif not shop.slug:
        shop.slug = await free_slug(session, slugify(f"{shop.name} {shop.city or ''}"), shop.id)
    vitrine = Vitrine(
        shop_id=shop.id,
        status="draft",
        source=source,
        partner_id=partner.id if partner is not None else None,
        hours=hours,
        created_by=admin.id,
    )
    session.add(vitrine)
    await session.flush()
    if hours is not None:
        shop.opening_hours = osm_opening_hours(hours)
    if source == "manual" and transformed:
        await _copy_proposal_menu(session, shop.id)
    if partner is not None:
        await vitrine_catalog.apply_stored(session, vitrine)
    await session.flush()
    if request_row is not None:
        await vitrine_requests.mark_accepted(session, admin, request_row, shop.id)
    await session.refresh(shop)
    await session.refresh(vitrine)
    return {"success": True, "vitrine": await vitrine_json(session, shop, vitrine)}


async def _copy_proposal_menu(session: AsyncSession, shop_id: uuid.UUID) -> None:
    """A customer's proposal becomes a Vitrine: its menu items become hand-made products."""
    items = (
        await session.execute(
            select(ShopMenuItem).where(ShopMenuItem.shop_id == shop_id).order_by(ShopMenuItem.position)
        )
    ).scalars()
    for position, item in enumerate(items):
        price = money(item.price)
        name = clean(item.name, NAME_MAX)
        if price is None or not name:
            continue
        session.add(
            VitrineProduct(
                shop_id=shop_id,
                name=name,
                description=clean(item.description, DESCRIPTION_MAX) or None,
                price=price,
                photo_key=item.photo_key,
                available=True,
                position=position,
            )
        )


async def update_vitrine(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    if "name" in payload:
        name = clean(payload["name"], NAME_MAX)
        if len(name) < 2:
            raise invalid("name: 2 to 100 characters")
        shop.name = name
    if "address" in payload:
        address = clean(payload["address"], 300)
        if len(address) < 3:
            raise invalid("address: 3 to 300 characters")
        shop.address = address
    for field_name, limit in (("city", 120), ("governorate", 120), ("description", 500)):
        if field_name in payload:
            setattr(shop, field_name, clean(payload[field_name], limit) or None)
    if "phone" in payload:
        try:
            shop.phone = shop_service._phone(payload["phone"]) if payload["phone"] else None
        except ApiError:
            raise invalid("phone: 8 Tunisian digits") from None
    if "category" in payload:
        category = payload["category"] or ""
        if category and category not in shop_service.CATEGORIES:
            raise invalid("category: unknown")
        shop.categories = [category] if category else []
    if "photo_url" in payload:
        shop.photo_key = shop_service.stored_photo(payload["photo_url"]) if payload["photo_url"] else None
    if "lat" in payload or "lng" in payload:
        lat, lng = payload.get("lat"), payload.get("lng")
        if (
            not isinstance(lat, int | float)
            or not isinstance(lng, int | float)
            or not within(lat, lng, ORDER_BOUNDS)
        ):
            raise invalid("lat / lng: a position in Tunisia")
        shop.location = f"SRID=4326;POINT({lng} {lat})"
    if "hours" in payload:
        vitrine.hours = clean_hours(payload["hours"])
        shop.opening_hours = osm_opening_hours(vitrine.hours)
    if payload.get("slug"):
        slug = _wanted_slug(payload["slug"])
        if (await session.execute(select(Shop.id).where(Shop.slug == slug, Shop.id != shop.id))).first():
            raise ApiError(409, "slug_taken", "This address is taken")
        shop.slug = slug
    if payload.get("apply_ods_identity") is True:
        snapshot = await _partner_catalog(session, vitrine.partner_id)
        restaurant = (snapshot.payload or {}).get("restaurant") if snapshot is not None else None
        if vitrine.source != "ods" or not restaurant:
            raise ApiError(409, "not_linked", "No ODS restaurant data to apply")
        if restaurant.get("name"):
            shop.name = clean(restaurant["name"], NAME_MAX)
        if restaurant.get("address"):
            shop.address = clean(restaurant["address"], 300)
        if restaurant.get("phone"):
            with contextlib.suppress(ApiError):  # not a Tunisian number: the shop keeps its own
                shop.phone = shop_service._phone(restaurant["phone"])
        r_lat, r_lng = restaurant.get("lat"), restaurant.get("lng")
        if r_lat is not None and r_lng is not None and within(r_lat, r_lng, ORDER_BOUNDS):
            shop.location = f"SRID=4326;POINT({r_lng} {r_lat})"
    await session.flush()
    await session.refresh(shop)
    await session.refresh(vitrine)
    return {"success": True, "vitrine": await vitrine_json(session, shop, vitrine)}


def _manual(vitrine: Vitrine) -> None:
    if vitrine.source != "manual":
        raise ApiError(409, "catalog_locked", "The catalog comes from ODS: change it in ODS")


def _position(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, min(value, 100_000))


async def _category(session: AsyncSession, shop_id: uuid.UUID, raw_id: Any) -> VitrineCategory:
    cid = as_uuid(raw_id)
    category = await session.get(VitrineCategory, cid) if cid is not None else None
    if category is None or category.shop_id != shop_id or category.archived_at is not None:
        raise ApiError(404, "category_not_found", "Category not found")
    return category


async def _product(session: AsyncSession, shop_id: uuid.UUID, raw_id: Any) -> VitrineProduct:
    pid = as_uuid(raw_id)
    product = await session.get(VitrineProduct, pid) if pid is not None else None
    if product is None or product.shop_id != shop_id or product.archived_at is not None:
        raise ApiError(404, "product_not_found", "Product not found")
    return product


async def _next_position(session: AsyncSession, model: Any, shop_id: uuid.UUID) -> int:
    top = (
        await session.execute(
            select(func.max(model.position)).where(model.shop_id == shop_id, model.archived_at.is_(None))
        )
    ).scalar_one()
    return 0 if top is None else top + 1


async def upsert_category(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    _manual(vitrine)
    name = clean(payload.get("name"), CATEGORY_NAME_MAX)
    if not name:
        raise invalid(f"name: 1 to {CATEGORY_NAME_MAX} characters")
    if payload.get("id"):
        category = await _category(session, shop.id, payload["id"])
        category.name = name
        if "position" in payload:
            category.position = _position(payload["position"], category.position)
    else:
        category = VitrineCategory(
            shop_id=shop.id,
            name=name,
            position=_position(
                payload.get("position"), await _next_position(session, VitrineCategory, shop.id)
            ),
        )
        session.add(category)
    await session.flush()
    return {"success": True, "category": category_json(category, admin=True)}


async def delete_category(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    _manual(vitrine)
    category = await _category(session, shop.id, payload.get("id"))
    category.archived_at = datetime.now(UTC)
    for product in (
        await session.execute(select(VitrineProduct).where(VitrineProduct.category_id == category.id))
    ).scalars():
        product.category_id = None
    await session.flush()
    return {"success": True}


async def upsert_product(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    _manual(vitrine)
    product = await _product(session, shop.id, payload["id"]) if payload.get("id") else None
    if product is None or "name" in payload:
        name = clean(payload.get("name"), NAME_MAX)
        if not name:
            raise invalid(f"name: 1 to {NAME_MAX} characters")
    else:
        name = product.name
    if product is None or "price" in payload:
        price = money(payload.get("price"))
        if price is None:
            raise invalid("price: 0 to 2000 DT")
    else:
        price = product.price
    category_id = None
    if payload.get("category_id"):
        category_id = (await _category(session, shop.id, payload["category_id"])).id
    elif product is not None and "category_id" not in payload:
        category_id = product.category_id
    if product is None:
        product = VitrineProduct(
            shop_id=shop.id,
            position=_position(
                payload.get("position"), await _next_position(session, VitrineProduct, shop.id)
            ),
            available=True,
        )
        session.add(product)
    elif "position" in payload:
        product.position = _position(payload["position"], product.position)
    product.name, product.price, product.category_id = name, price, category_id
    if "description" in payload:
        product.description = clean(payload["description"], DESCRIPTION_MAX) or None
    if "photo_url" in payload:
        product.photo_key = shop_service.stored_photo(payload["photo_url"]) if payload["photo_url"] else None
    if "available" in payload:
        if not isinstance(payload["available"], bool):
            raise invalid("available: true or false")
        product.available = payload["available"]
    await session.flush()
    await session.refresh(product)
    return {"success": True, "product": await _admin_product(session, shop, product)}


async def _admin_product(session: AsyncSession, shop: Shop, product: VitrineProduct) -> dict[str, Any]:
    """One product as getVitrineAdmin shows it (its chips ranked with the whole shop's counts)."""
    categories = await _active_rows(session, VitrineCategory, shop.id)
    products = await _active_rows(session, VitrineProduct, shop.id)
    if all(p.id != product.id for p in products):
        products.append(product)
    options = await vitrine_options.shop_options(session, shop, products, categories)
    names = {c.id: c.name for c in categories}
    return admin_product_json(shop, product, names.get(product.category_id), options.get(product.id))


async def delete_product(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    _manual(vitrine)
    product = await _product(session, shop.id, payload.get("id"))
    product.archived_at = datetime.now(UTC)
    await session.flush()
    return {"success": True}


async def reorder(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    _manual(vitrine)
    for key, loader in (("categories", _category), ("products", _product)):
        ids = payload.get(key)
        if ids is None:
            continue
        if not isinstance(ids, list) or len(ids) > 1000:
            raise invalid(f"{key}: a list of ids")
        for position, raw_id in enumerate(ids):
            (await loader(session, shop.id, raw_id)).position = position
    await session.flush()
    return {"success": True}


async def set_visibility(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    wanted = payload.get("visibility")
    if wanted not in VISIBILITIES:
        raise invalid("visibility: qa or public")
    if wanted == "public":
        if not await public_allowed(session):
            raise ApiError(409, "public_not_allowed", "« Vitrines publiques autorisées » is off")
        if vitrine.catalog_test:
            raise ApiError(409, "catalog_test", "The ODS restaurant is in test mode")
        checks = await checklist(session, shop, vitrine)
        missing = [k for k, ok in checks.items() if not ok]
        if missing:
            raise ApiError(409, "checklist_incomplete", "Checklist incomplete", missing=missing)
    shop.visibility = wanted
    await session.flush()
    await session.refresh(shop)
    return {"success": True, "vitrine": await vitrine_json(session, shop, vitrine)}


async def set_status(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    wanted = payload.get("status")
    if wanted not in STATUSES:
        raise invalid("status: draft, active, paused or archived")
    vitrine.status = wanted
    await session.flush()
    await session.refresh(vitrine)
    return {"success": True, "vitrine": await vitrine_json(session, shop, vitrine)}


async def request_sync(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    from app.services import partner_webhooks

    _admin(admin)
    shop, vitrine = await load(session, payload.get("shop_id"))
    partner = await session.get(Partner, vitrine.partner_id) if vitrine.partner_id else None
    if vitrine.source != "ods" or partner is None or not partner.active:
        raise ApiError(409, "not_linked", "This Vitrine is not linked to an active ODS restaurant")
    if not partner.webhook_url:
        raise ApiError(409, "webhook_not_configured", "No webhook URL on this partner")
    delivery = await partner_webhooks.queue(
        session, partner, "catalog.sync_requested", None, {"reason": "admin_refresh", "shop_id": str(shop.id)}
    )
    assert delivery is not None
    sent = await partner_webhooks.deliver_now(session, delivery)
    return {"success": True, "delivered": sent["delivered"], "delivery_id": sent["delivery_id"]}


async def list_partner_catalogs(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    rows = (
        await session.execute(
            select(Partner, PartnerCatalog, Vitrine, Shop)
            .outerjoin(PartnerCatalog, PartnerCatalog.partner_id == Partner.id)
            .outerjoin(Vitrine, Vitrine.partner_id == Partner.id)
            .outerjoin(Shop, Shop.id == Vitrine.shop_id)
            .where(Partner.active.is_(True), Partner.kind == "ods_restaurant")
            .order_by(Partner.name)
        )
    ).all()
    catalogs = []
    for partner, snapshot, vitrine, shop in rows:
        payload_json = snapshot.payload if snapshot is not None else {}
        restaurant = payload_json.get("restaurant") or {}
        catalogs.append(
            {
                "partner": {
                    "id": str(partner.id),
                    "name": partner.name,
                    "ods_restaurant_id": partner.ods_restaurant_id,
                    "active": partner.active,
                },
                "catalog": {
                    "catalog_version": snapshot.catalog_version,
                    "test": snapshot.test,
                    "generated_at": iso(snapshot.generated_at),
                    "received_at": iso(snapshot.received_at),
                    "checked_at": iso(snapshot.checked_at),
                    "products": snapshot.products_count,
                    "categories": len(payload_json.get("categories") or []),
                    "restaurant": {k: restaurant.get(k) for k in ("name", "address", "phone", "lat", "lng")},
                }
                if snapshot is not None
                else None,
                "vitrine": {"shop_id": str(shop.id), "name": shop.name} if vitrine is not None else None,
            }
        )
    return {"success": True, "catalogs": catalogs}


# --- posters -----------------------------------------------------------------------------------------


async def create_poster(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    shop, _vitrine = await load(session, payload.get("shop_id"), lock=True)
    label = clean(payload.get("label"), 40)
    if not label:
        raise invalid("label: 1 to 40 characters")
    base = slugify(label)[:24] or "affiche"
    code, n = base, 1
    while await session.get(VitrinePoster, (shop.id, code)) is not None:
        n += 1
        code = f"{base}-{n}"
    poster = VitrinePoster(shop_id=shop.id, code=code, label=label)
    session.add(poster)
    await session.flush()
    return {"success": True, "poster": {"code": code, "label": label, "url": poster_url(shop, code)}}


async def delete_poster(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    shop, _vitrine = await load(session, payload.get("shop_id"), lock=True)
    code = str(payload.get("code") or "")[:32]
    poster = await session.get(VitrinePoster, (shop.id, code)) if code else None
    if poster is None or poster.archived_at is not None:
        raise ApiError(404, "poster_not_found", "Poster not found")
    poster.archived_at = datetime.now(UTC)
    await session.flush()
    return {"success": True}


# --- merchant link -----------------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def merchant_url(token: str) -> str:
    return app_url(f"/m/{token}")


async def rotate_merchant_link(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    _shop, vitrine = await load(session, payload.get("shop_id"), lock=True)
    token = secrets.token_urlsafe(32)
    vitrine.merchant_token_hash = token_hash(token)
    vitrine.merchant_token_enc = secret_box.encrypt(token)
    vitrine.merchant_link_no += 1
    vitrine.merchant_link_opens = 0
    vitrine.merchant_link_created_at = datetime.now(UTC)
    await session.flush()
    return {"success": True, "url": merchant_url(token), "merchant_link": merchant_link_json(vitrine)}


async def get_merchant_link(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    _shop, vitrine = await load(session, payload.get("shop_id"))
    token = secret_box.decrypt(vitrine.merchant_token_enc) if vitrine.merchant_token_enc else None
    return {
        "success": True,
        "url": merchant_url(token) if token else None,
        "merchant_link": merchant_link_json(vitrine),
    }


# --- client reads ------------------------------------------------------------------------------------


async def ordering_state(
    session: AsyncSession, vitrine: Vitrine, state: dict[str, Any], available_products: int
) -> dict[str, Any]:
    reason = None
    if available_products == 0:
        reason = "no_product"
    elif state["open_now"] is False:
        reason = "shop_closed"
    elif vitrine.source == "ods" and vitrine.partner_id is not None:
        partner = await session.get(Partner, vitrine.partner_id)
        if partner is not None and partner.debt_amount and partner.debt_amount > 0:
            reason = "restaurant_debt_unpaid"
    return {"allowed": reason is None, "reason": reason}


async def client_json(session: AsyncSession, shop: Shop, vitrine: Vitrine) -> dict[str, Any]:
    from app.services import vitrine_pickup

    lat, lng = await _coords(session, shop)
    categories = await _active_rows(session, VitrineCategory, shop.id)
    products = await _active_rows(session, VitrineProduct, shop.id)
    options = await vitrine_options.shop_options(session, shop, products, categories)
    snapshot = await _partner_catalog(session, vitrine.partner_id) if vitrine.source == "ods" else None
    state = open_state(vitrine.hours)
    allowed = await public_allowed(session)
    updated = vitrine.catalog_synced_at if vitrine.source == "ods" else None
    if updated is None:
        updated = max((p.updated_at for p in products), default=vitrine.updated_at)
    return {
        "shop_id": str(shop.id),
        "slug": shop.slug,
        "name": shop.name,
        "address": shop.address,
        "city": shop.city,
        "governorate": shop.governorate,
        "phone": shop.phone,
        "lat": lat,
        "lng": lng,
        "category": shop.categories[0] if shop.categories else None,
        "photo_url": shop_service.photo_url_of(shop.photo_key),
        "description": shop.description,
        "opening_hours": shop.opening_hours,
        "hours": vitrine.hours,
        **state,
        "source": vitrine.source,
        "test": not effectively_public(shop, vitrine, allowed),
        "catalog_updated_at": iso(updated),
        "catalog_stale": catalog_stale(vitrine, snapshot.checked_at if snapshot is not None else None),
        "ordering": await ordering_state(session, vitrine, state, sum(1 for p in products if p.available)),
        "pickup": await vitrine_pickup.info(session, vitrine, state),
        "categories": [category_json(c) for c in categories],
        "products": [product_json(p, options=options.get(p.id)) for p in products],
    }


async def get_vitrine(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any], client_key: str
) -> dict[str, Any]:
    """getVitrine (client, account optional): the page /v/<slug>; counts the visit and the scan."""
    from app.services import vitrine_stats

    shop, vitrine = await find_visible(
        session, viewer, shop_id=payload.get("shop_id"), slug=payload.get("slug")
    )
    allowed = await public_allowed(session)
    kind = viewer_kind(viewer)
    counted = kind != "admin" and (kind != "qa" or not effectively_public(shop, vitrine, allowed))
    if counted:
        visitor = vitrine_stats.visitor_key(viewer, payload.get("device_id"), client_key)
        await vitrine_stats.record(session, shop.id, "view", visitor)
        src = payload.get("src")
        if isinstance(src, str) and src.strip():
            poster = await session.get(VitrinePoster, (shop.id, src.strip().lower()[:32]))
            if poster is not None:
                await vitrine_stats.record(session, shop.id, f"scan:{poster.code}", visitor)
    return {"success": True, "vitrine": await client_json(session, shop, vitrine)}


async def list_nearby(session: AsyncSession, viewer: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """listVitrinesNearby: the visible active Vitrines around a point, nearest first."""
    from app.services.places import point

    lat, lng = payload.get("lat"), payload.get("lng")
    if (
        not isinstance(lat, int | float)
        or not isinstance(lng, int | float)
        or not within(lat, lng, ORDER_BOUNDS)
    ):
        raise invalid("lat / lng: a position in Tunisia")
    radius = payload.get("radius_km", 10)
    radius = float(radius) if isinstance(radius, int | float) and not isinstance(radius, bool) else 10.0
    radius = max(0.5, min(radius, 30.0))
    limit = payload.get("limit", 20)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 50 else 20
    distance = func.ST_Distance(Shop.location, point(lat, lng), False)
    rows = (
        await session.execute(
            select(Shop, Vitrine, (distance / 1000.0).label("km"))
            .join(Vitrine, Vitrine.shop_id == Shop.id)
            .where(
                Vitrine.status == "active",
                shop_visible_sql(viewer, Shop.id, Shop.visibility),
                func.ST_DWithin(Shop.location, point(lat, lng), radius * 1000.0, False),
            )
            .order_by(distance, Shop.id)
            .limit(limit)
        )
    ).all()
    allowed = await public_allowed(session)
    out = []
    for shop, vitrine, km in rows:
        s_lat, s_lng = await _coords(session, shop)
        preview = (
            await session.execute(
                select(VitrineProduct)
                .where(
                    VitrineProduct.shop_id == shop.id,
                    VitrineProduct.archived_at.is_(None),
                    VitrineProduct.available.is_(True),
                )
                .order_by(VitrineProduct.position, VitrineProduct.created_at)
                .limit(3)
            )
        ).scalars()
        state = open_state(vitrine.hours)
        out.append(
            {
                "shop_id": str(shop.id),
                "slug": shop.slug,
                "name": shop.name,
                "category": shop.categories[0] if shop.categories else None,
                "lat": s_lat,
                "lng": s_lng,
                "distance_km": round(float(km), 2),
                "address": shop.address,
                "city": shop.city,
                "photo_url": shop_service.photo_url_of(shop.photo_key),
                "open_now": state["open_now"],
                "opens_at": state["opens_at"],
                "closes_at": state["closes_at"],
                "source": vitrine.source,
                "test": not effectively_public(shop, vitrine, allowed),
                "preview": [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "price": float(p.price),
                        "photo_url": shop_service.photo_url_of(p.photo_key),
                    }
                    for p in preview
                ],
            }
        )
    return {"success": True, "vitrines": out}
