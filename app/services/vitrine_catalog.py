"""The ODS catalog of a linked Vitrine: PUT /api/partner/v1/catalog (contract §6) and the switch
between the two catalog sources (setVitrineCatalogSource, contract §3 / SPEC §5).

ODS pushes a FULL snapshot. Delivery computes its own fingerprint (sha256 of the canonical JSON of
test + restaurant + categories + products; `generated_at` and `catalog_version` left out): the same
fingerprint as the last one → `unchanged`, nothing of the catalog is written (only `checked_at`, at
most hourly). Otherwise the snapshot is kept in `partner_catalogs` and applied to the linked Vitrine:
categories and products upserted by ODS id, the missing ones archived, the ones back un-archived;
the hours rewritten; a `test` snapshot locks the Vitrine in 'qa'. Name, address, position and
phone of the shop are never overwritten (the admin applies them, updateVitrine apply_ods_identity).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Partner, PartnerCatalog, Shop, Vitrine, VitrineCategory, VitrineProduct
from app.security.deps import CurrentUser
from app.services import vitrine as vitrine_service
from app.services import vitrine_extras, vitrine_options

MAX_PRODUCTS = 300
MAX_CATEGORIES = 100
CHECKED_EVERY = timedelta(hours=1)


class HoursRuleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    days: list[int] = Field(min_length=1, max_length=7)
    open: str
    close: str


class HoursIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tz: str | None = "Africa/Tunis"
    weekly: list[HoursRuleIn] = Field(default_factory=list, max_length=21)


class RestaurantIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=500)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    hours: HoursIn | None = None


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ods_category_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    position: int = Field(default=0, ge=0, le=100_000)


class IngredientIn(BaseModel):
    """One ingredient of the dish's recipe in ODS (options « ce que j'enlève », CONTRAT-OPTIONS §1)."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200)
    removable: bool | None = None


class ExtraChoiceIn(BaseModel):
    """One priced answer of a dish's question in ODS (paid extras, CONTRAT-AJOUTS §1)."""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    price: float | str | None = None


class ExtraGroupIn(BaseModel):
    """One question of a dish in ODS (« Suppléments », « Sauce »): one choice or several."""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    multiple: bool = False
    choices: list[ExtraChoiceIn] = Field(default_factory=list, max_length=40)


class ProductIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ods_menu_id: int = Field(gt=0)
    ods_category_id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    price: float = Field(ge=0, le=2000)
    photo_url: str | None = Field(default=None, max_length=1000)
    available: bool = True
    position: int = Field(default=0, ge=0, le=100_000)
    # absent = no recipe (an ODS that does not send it keeps its fingerprint)
    ingredients: list[IngredientIn] | None = Field(default=None, max_length=60)
    # absent = no question on the dish in ODS (an ODS that does not send it keeps its fingerprint)
    extras: list[ExtraGroupIn] | None = Field(default=None, max_length=12)

    @field_validator("price")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("price must be a number")
        return value


class CatalogIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    catalog_version: str | None = Field(default=None, max_length=128)
    test: bool = False
    generated_at: datetime | None = None
    restaurant: RestaurantIn
    categories: list[CategoryIn] = Field(default_factory=list, max_length=MAX_CATEGORIES)
    products: list[ProductIn] = Field(default_factory=list, max_length=MAX_PRODUCTS)


def parse(raw: Any) -> CatalogIn:
    """The body, validated: 413 too_many_products before anything else, 400 validation_error."""
    if not isinstance(raw, dict):
        raise ApiError(400, "validation_error", "A JSON object is expected")
    products = raw.get("products")
    if isinstance(products, list) and len(products) > MAX_PRODUCTS:
        raise ApiError(413, "too_many_products", f"At most {MAX_PRODUCTS} products", max=MAX_PRODUCTS)
    try:
        body = CatalogIn.model_validate(raw)
    except ValidationError as exc:
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "message": err.get("msg", "")}
            for err in exc.errors()[:20]
        ]
        raise ApiError(400, "validation_error", "Invalid catalog", details=details) from None
    if len({c.ods_category_id for c in body.categories}) != len(body.categories):
        raise ApiError(400, "validation_error", "ods_category_id: duplicated")
    if len({p.ods_menu_id for p in body.products}) != len(body.products):
        raise ApiError(400, "validation_error", "ods_menu_id: duplicated")
    if body.restaurant.hours is not None:
        try:
            vitrine_service.clean_hours(body.restaurant.hours.model_dump())
        except ApiError as exc:
            raise ApiError(400, "validation_error", f"restaurant.{exc.message}") from None
    return body


def normalized(body: CatalogIn) -> dict[str, Any]:
    """What Delivery keeps and fingerprints: cut to its limits, sorted by ODS id."""
    clean = vitrine_service.clean
    restaurant = body.restaurant
    hours = (
        vitrine_service.clean_hours(restaurant.hours.model_dump()) if restaurant.hours is not None else None
    )
    return {
        "test": body.test,
        "restaurant": {
            "name": clean(restaurant.name, vitrine_service.NAME_MAX),
            "phone": clean(restaurant.phone, 40) or None,
            "address": clean(restaurant.address, 300) or None,
            "lat": restaurant.lat,
            "lng": restaurant.lng,
            "hours": hours,
        },
        "categories": sorted(
            (
                {
                    "ods_category_id": c.ods_category_id,
                    "name": clean(c.name, vitrine_service.CATEGORY_NAME_MAX) or "—",
                    "position": c.position,
                }
                for c in body.categories
            ),
            key=lambda c: c["ods_category_id"],
        ),
        "products": sorted(
            (
                {
                    **_ingredients(p),
                    **_extras(p),
                    "ods_menu_id": p.ods_menu_id,
                    "ods_category_id": p.ods_category_id,
                    "name": clean(p.name, vitrine_service.NAME_MAX) or "—",
                    "description": clean(p.description, vitrine_service.DESCRIPTION_MAX) or None,
                    "price": str(vitrine_service.money(p.price)),
                    "photo_url": p.photo_url.strip()
                    if isinstance(p.photo_url, str) and p.photo_url.strip().startswith("https://")
                    else None,
                    "available": p.available,
                    "position": p.position,
                }
                for p in body.products
            ),
            key=lambda p: p["ods_menu_id"],
        ),
    }


def _ingredients(p: ProductIn) -> dict[str, Any]:
    """{"ingredients": [{name, removable}]} cut to 30 distinct names, or nothing when ODS sent none."""
    if p.ingredients is None:
        return {}
    out, seen = [], set()
    for ing in p.ingredients:
        name = vitrine_service.clean(ing.name, vitrine_options.INGREDIENT_NAME_MAX)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "removable": ing.removable})
    return {"ingredients": out[: vitrine_options.MAX_INGREDIENTS]}


def _extras(p: ProductIn) -> dict[str, Any]:
    """{"extras": [groups]} (8 × 20 at most, CONTRAT-AJOUTS §1), or nothing when ODS sent none or none is
    left."""
    if p.extras is None:
        return {}
    groups = vitrine_extras.clean_groups([g.model_dump() for g in p.extras])
    return {"extras": groups} if groups else {}


def content_hash(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


async def receive(session: AsyncSession, partner: Partner, raw: Any) -> dict[str, Any]:
    """PUT /catalog for `partner` (key already checked)."""
    body = parse(raw)
    content = normalized(body)
    fingerprint = content_hash(content)
    now = datetime.now(UTC)
    # one snapshot at a time per restaurant
    await session.execute(select(Partner.id).where(Partner.id == partner.id).with_for_update())
    snapshot = await session.get(PartnerCatalog, partner.id)
    vitrine = (
        await session.execute(
            select(Vitrine)
            .where(Vitrine.partner_id == partner.id, Vitrine.source == "ods", Vitrine.status != "archived")
            .with_for_update()
        )
    ).scalar_one_or_none()
    linked = {"shop_id": str(vitrine.shop_id)} if vitrine is not None else None
    if snapshot is not None and snapshot.content_hash == fingerprint:
        if now - snapshot.checked_at >= CHECKED_EVERY:
            snapshot.checked_at = now
            await session.flush()
        return {
            "status": "unchanged",
            "catalog_version": body.catalog_version or snapshot.catalog_version,
            "products": snapshot.products_count,
            "vitrine": linked,
        }
    if snapshot is None:
        snapshot = PartnerCatalog(partner_id=partner.id)
        session.add(snapshot)
    snapshot.content_hash = fingerprint
    snapshot.catalog_version = body.catalog_version or fingerprint
    snapshot.test = body.test
    snapshot.generated_at = body.generated_at
    snapshot.payload = content
    snapshot.products_count = len(content["products"])
    snapshot.received_at = snapshot.checked_at = now
    await session.flush()
    if vitrine is not None:
        await apply(session, vitrine, snapshot)
    return {
        "status": "updated",
        "catalog_version": snapshot.catalog_version,
        "products": snapshot.products_count,
        "vitrine": linked,
    }


def _set(row: Any, **values: Any) -> bool:
    """Assigns only what differs (an unchanged row is not rewritten)."""
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


async def apply(session: AsyncSession, vitrine: Vitrine, snapshot: PartnerCatalog) -> None:
    """Applies the stored snapshot to the Vitrine (locked by the caller)."""
    content = snapshot.payload or {}
    now = datetime.now(UTC)
    categories = {
        c.ods_category_id: c
        for c in (
            await session.execute(
                select(VitrineCategory).where(
                    VitrineCategory.shop_id == vitrine.shop_id, VitrineCategory.ods_category_id.is_not(None)
                )
            )
        ).scalars()
    }
    seen_categories = set()
    for incoming in content.get("categories") or []:
        ods_id = incoming["ods_category_id"]
        seen_categories.add(ods_id)
        row = categories.get(ods_id)
        if row is None:
            row = VitrineCategory(shop_id=vitrine.shop_id, ods_category_id=ods_id, name=incoming["name"])
            session.add(row)
            categories[ods_id] = row
        _set(row, name=incoming["name"], position=incoming["position"], archived_at=None)
    for ods_id, row in categories.items():
        if ods_id not in seen_categories and row.archived_at is None:
            row.archived_at = now
    await session.flush()

    products = {
        p.ods_menu_id: p
        for p in (
            await session.execute(
                select(VitrineProduct).where(
                    VitrineProduct.shop_id == vitrine.shop_id, VitrineProduct.ods_menu_id.is_not(None)
                )
            )
        ).scalars()
    }
    seen_products = set()
    for incoming in content.get("products") or []:
        ods_id = incoming["ods_menu_id"]
        seen_products.add(ods_id)
        category = categories.get(incoming.get("ods_category_id"))
        values = {
            "name": incoming["name"],
            "description": incoming.get("description"),
            "price": Decimal(incoming["price"]),
            "photo_key": incoming.get("photo_url"),
            "available": incoming["available"],
            "position": incoming["position"],
            "category_id": category.id if category is not None and category.archived_at is None else None,
            "ods_ingredients": incoming.get("ingredients"),
            "ods_extras": incoming.get("extras"),
            "archived_at": None,
        }
        row = products.get(ods_id)
        if row is None:
            row = VitrineProduct(shop_id=vitrine.shop_id, ods_menu_id=ods_id, **values)
            session.add(row)
            products[ods_id] = row
        else:
            _set(row, **values)
    for ods_id, row in products.items():
        if ods_id not in seen_products and row.archived_at is None:
            row.archived_at = now

    hours = (content.get("restaurant") or {}).get("hours")
    shop = await session.get(Shop, vitrine.shop_id, with_for_update=True)
    assert shop is not None
    if hours:
        _set(vitrine, hours=hours)
        _set(shop, opening_hours=vitrine_service.osm_opening_hours(hours))
    vitrine.catalog_version = snapshot.catalog_version
    vitrine.catalog_synced_at = now
    vitrine.catalog_test = snapshot.test
    if snapshot.test and shop.visibility == "public":
        shop.visibility = "qa"  # an ODS test restaurant never shows to the public
    await session.flush()


async def apply_stored(session: AsyncSession, vitrine: Vitrine) -> bool:
    """At linking time: the snapshot already received (if any) applies at once."""
    snapshot = await session.get(PartnerCatalog, vitrine.partner_id) if vitrine.partner_id else None
    if snapshot is None:
        return False
    await apply(session, vitrine, snapshot)
    return True


# --- setVitrineCatalogSource -------------------------------------------------------------------------


async def _count(session: AsyncSession, shop_id: uuid.UUID, *where: Any) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(VitrineProduct)
                .where(VitrineProduct.shop_id == shop_id, *where)
            )
        ).scalar_one()
    )


async def _rows(session: AsyncSession, model: Any, shop_id: uuid.UUID, *where: Any) -> list[Any]:
    return list((await session.execute(select(model).where(model.shop_id == shop_id, *where))).scalars())


async def set_source(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    vitrine_service._admin(admin)
    shop, vitrine = await vitrine_service.load(session, payload.get("shop_id"), lock=True)
    source = payload.get("source")
    dry_run = payload.get("dry_run") is True
    now = datetime.now(UTC)
    current = await _count(session, shop.id, VitrineProduct.archived_at.is_(None))
    if source == "ods":
        partner = await vitrine_service.linkable_partner(session, payload.get("partner_id"), shop.id)
        snapshot = await session.get(PartnerCatalog, partner.id)
        if dry_run:
            incoming = snapshot.products_count if snapshot is not None else 0
            return {"success": True, "preview": {"incoming_products": incoming, "current_products": current}}
        if vitrine.source == "manual" or vitrine.partner_id != partner.id:
            # the hand-made catalog (or another restaurant's) is put aside
            for model in (VitrineProduct, VitrineCategory):
                for row in await _rows(session, model, shop.id, model.archived_at.is_(None)):
                    row.archived_at = now
            if vitrine.source == "manual":
                vitrine.manual_archived_at = now
        vitrine.source, vitrine.partner_id = "ods", partner.id
        await session.flush()
        await apply_stored(session, vitrine)
    elif source == "manual":
        mode = payload.get("mode")
        if mode not in ("keep", "empty", "restore"):
            raise vitrine_service.invalid("mode: keep, empty or restore")
        restorable = vitrine.manual_archived_at is not None and now - vitrine.manual_archived_at <= (
            vitrine_service.RESTORE_WINDOW
        )
        if mode == "restore" and not restorable:
            raise ApiError(409, "restore_unavailable", "No hand-made catalog to bring back (30 days)")
        if dry_run:
            incoming = {
                "keep": current,
                "empty": 0,
                "restore": await _count(
                    session,
                    shop.id,
                    VitrineProduct.archived_at == vitrine.manual_archived_at,
                    VitrineProduct.ods_menu_id.is_(None),
                )
                if restorable
                else 0,
            }[mode]
            return {"success": True, "preview": {"incoming_products": incoming, "current_products": current}}
        for model, ods_column in ((VitrineProduct, "ods_menu_id"), (VitrineCategory, "ods_category_id")):
            for row in await _rows(session, model, shop.id, model.archived_at.is_(None)):
                if mode == "keep":
                    setattr(row, ods_column, None)  # a plain hand-made copy, no ODS link any more
                else:
                    row.archived_at = now
            if mode == "restore":
                for row in await _rows(
                    session,
                    model,
                    shop.id,
                    model.archived_at == vitrine.manual_archived_at,
                    getattr(model, ods_column).is_(None),
                ):
                    row.archived_at = None
        vitrine.source, vitrine.partner_id = "manual", None
        vitrine.catalog_version, vitrine.catalog_test = None, False
        vitrine.manual_archived_at = None  # « restore » is offered when coming back, not later
        await session.flush()
    else:
        raise vitrine_service.invalid("source: manual or ods")
    await session.refresh(shop)
    await session.refresh(vitrine)
    return {"success": True, "vitrine": await vitrine_service.vitrine_json(session, shop, vitrine)}
