"""DeliveryTariffs: retired (DB_AUDIT §1.11 / §4.4: 0 rows, no function reads it, the fee is
the courier's offer). Registered only so the admin tab (TariffSettings.jsx) keeps working:
reads answer an empty list (GET /id → 404), every write answers
410 { error: 'retired' } (the tab shows its "save failed" toast; nothing crashes).
"""

from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Text, false, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.security.deps import CurrentUser

MESSAGE = "DeliveryTariffs is retired: the delivery fee is the courier's offer"

# An always-empty relation with the legacy columns (no table behind it).
_empty = (
    select(
        literal(None, Text).label("id"),
        literal(None, Text).label("name"),
        literal(None, Float).label("price_per_km"),
        literal(None, Float).label("min_fee"),
        literal(None, Text).label("commission_type"),
        literal(None, Float).label("commission_percentage"),
        literal(None, Float).label("commission_amount"),
        literal(None, Boolean).label("is_active"),
        literal(None, Text).label("description"),
        literal(None, DateTime(timezone=True)).label("created_at"),
    )
    .where(false())
    .subquery("delivery_tariffs_retired")
)


def _retired() -> ApiError:
    return ApiError(410, "retired", MESSAGE)


async def create(session: AsyncSession, actor: CurrentUser, data: dict[str, Any]) -> str:
    raise _retired()


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    raise _retired()


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> None:
    raise _retired()


ENTITY = register(
    EntityDef(
        name="DeliveryTariffs",
        source=_empty,
        id_expr=_empty.c.id,
        id_type="text",
        created_expr=_empty.c.created_at,
        updated_expr=_empty.c.created_at,
        fields={
            "name": LegacyField(_empty.c.name, "string"),
            "price_per_km": LegacyField(_empty.c.price_per_km, "number"),
            "min_fee": LegacyField(_empty.c.min_fee, "number"),
            "commission_type": LegacyField(_empty.c.commission_type, "string"),
            "commission_percentage": LegacyField(_empty.c.commission_percentage, "number"),
            "commission_amount": LegacyField(_empty.c.commission_amount, "number"),
            "is_active": LegacyField(_empty.c.is_active, "boolean"),
            "description": LegacyField(_empty.c.description, "string"),
        },
        read_policy=lambda _user: true(),
        create=create,
        update=update,
        delete=delete,
    )
)
