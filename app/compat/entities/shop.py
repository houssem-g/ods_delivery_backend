"""Shop: approved shops and user proposals.

Read: approved shops for everybody signed in; a proposal (pending / rejected) only for
its author; admins read everything (Admin → Magasins lists the pending ones).
`proposed_by` (author e-mail) is visible to the author and admins only.
Create: none from the front (proposals go through proposeShop) → 403.
Update: admins — review_status ('approved' | 'rejected' | 'pending', + reviewed_by /
reviewed_at) and the descriptive fields. The live code sends no notification on a
decision, so none is sent. Delete: admins.
"""

import uuid
from typing import Any

from sqlalchemy import Text, case, func, literal, or_, select, text, true
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Shop, ShopMenuItem, User
from app.security.deps import CurrentUser
from app.security.tokens import now_utc
from app.services.places import lat_expr, lng_expr
from app.services.shops import public_prefix

shops = Shop.__table__
menu = ShopMenuItem.__table__
proposer = User.__table__.alias("shop_proposer")

REVIEW_STATUSES = ("pending", "approved", "rejected")
TEXT_FIELDS = {
    "name": 100, "address": 300, "phone": 32, "opening_hours": 100, "description": 500, "city": 120,
    "governorate": 120,
}  # fmt: skip
ADMIN_FIELDS = frozenset({"review_status", *TEXT_FIELDS})


def photo_expr(column: Any) -> Any:
    """Stored key → public URL; a legacy absolute URL is answered as is."""
    return case(
        (column.is_(None), None),
        (column.like("http%"), column),
        else_=func.concat(literal(public_prefix(), Text), column),
    )


def _menu_items() -> Any:
    item = func.jsonb_build_object(
        "name", menu.c.name, "price", menu.c.price, "photo_url", photo_expr(menu.c.photo_key),
        "description", menu.c.description, type_=JSONB,
    )  # fmt: skip
    return (
        select(func.coalesce(func.jsonb_agg(aggregate_order_by(item, menu.c.position)), text("'[]'::jsonb")))
        .where(menu.c.shop_id == shops.c.id)
        .scalar_subquery()
    )


def _author_or_admin(user: CurrentUser) -> Any:
    return true() if user.is_admin else shops.c.proposed_by == user.id


def _read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    return or_(shops.c.review_status == "approved", shops.c.proposed_by == user.id)


FIELDS: dict[str, LegacyField] = {
    "name": LegacyField(shops.c.name, "string"),
    "address": LegacyField(shops.c.address, "string"),
    "latitude": LegacyField(lat_expr(shops.c.location), "number"),
    "longitude": LegacyField(lng_expr(shops.c.location), "number"),
    "categories": LegacyField(shops.c.categories, "array"),
    "phone": LegacyField(shops.c.phone, "string"),
    "opening_hours": LegacyField(shops.c.opening_hours, "string"),
    "description": LegacyField(shops.c.description, "string"),
    "photo_url": LegacyField(photo_expr(shops.c.photo_key), "string"),
    "osm_id": LegacyField(shops.c.osm_id, "string"),
    "governorate": LegacyField(shops.c.governorate, "string"),
    "city": LegacyField(shops.c.city, "string"),
    "menu_items": LegacyField(_menu_items(), "array"),
    "review_status": LegacyField(shops.c.review_status, "string"),
    "proposed_by": LegacyField(proposer.c.email, "string", read_guard=_author_or_admin),
    "proposed_at": LegacyField(shops.c.proposed_at, "datetime"),
}


def _require_admin(actor: CurrentUser, operation: str) -> None:
    if not actor.is_admin:
        raise ApiError(403, "permission_denied", f"Permission denied for {operation} operation on Shop")


async def _load(session: AsyncSession, doc_id: str) -> Shop:
    try:
        shop_id = uuid.UUID(doc_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "Shop not found") from exc
    shop = await session.get(Shop, shop_id, with_for_update=True)
    if shop is None:
        raise ApiError(404, "not_found", "Shop not found")
    return shop


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    _require_admin(actor, "update")
    shop = await _load(session, doc_id)
    values = coerce_payload(ENTITY, data, ADMIN_FIELDS)
    if "review_status" in values:
        status = values["review_status"]
        if status not in REVIEW_STATUSES:
            raise ApiError(400, "validation_error", "review_status: expected pending, approved or rejected")
        if status != shop.review_status:
            shop.review_status = status
            shop.reviewed_by = actor.id
            shop.reviewed_at = now_utc()
    for name, limit in TEXT_FIELDS.items():
        if name in values:
            value = (values[name] or "").strip()[:limit]
            if name == "name" and len(value) < 2:
                raise ApiError(400, "validation_error", "name: at least 2 characters")
            setattr(shop, name, value if name == "name" else (value or None))
    await session.flush()


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> list[Any]:
    _require_admin(actor, "delete")
    shop = await _load(session, doc_id)
    audience = [actor.id] + ([shop.proposed_by] if shop.proposed_by else [])
    await session.delete(shop)
    await session.flush()
    return audience


ENTITY = register(
    EntityDef(
        name="Shop",
        source=shops.outerjoin(proposer, proposer.c.id == shops.c.proposed_by),
        id_expr=shops.c.id,
        id_type="uuid",
        created_expr=shops.c.created_at,
        updated_expr=shops.c.updated_at,
        fields=FIELDS,
        read_policy=_read_policy,
        update=update,
        delete=delete,
    )
)
