"""Entity registry of the compatibility layer.

Each legacy entity (UserProfile, Order, ...) declares how its document shape is
computed from the normalized tables, who may read which rows and fields, and
which writes exist. See docs/COMPAT_GUIDE.md for how to add one.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import ColumnElement, FromClause, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser

FieldType = Literal["string", "number", "integer", "boolean", "datetime", "object", "array", "id"]
Guard = Callable[[CurrentUser], ColumnElement[bool]]

# Write hooks. They validate with `compat.payload.coerce_payload`, call domain services and flush;
# the router emits the realtime event and commits. They raise ApiError to refuse.
CreateHook = Callable[[AsyncSession, CurrentUser, dict[str, Any]], Awaitable[str]]
UpdateHook = Callable[[AsyncSession, CurrentUser, str, dict[str, Any]], Awaitable[None]]
# A delete hook may return the user ids who should hear about the deletion (default: the caller).
DeleteHook = Callable[[AsyncSession, CurrentUser, str], Awaitable[Iterable[uuid.UUID] | None]]


@dataclass(frozen=True)
class LegacyField:
    """One legacy field: the SQL expression that computes it and how it may be used.

    `read_guard` hides the value (NULL) from users for whom the guard is false; it
    applies to filtering and sorting too, so a hidden field can't be probed.
    """

    expr: ColumnElement[Any]
    type: FieldType
    read_guard: Guard | None = None


@dataclass(frozen=True)
class EntityDef:
    name: str
    source: FromClause
    id_expr: ColumnElement[Any]
    id_type: Literal["uuid", "text"]
    created_expr: ColumnElement[Any]
    updated_expr: ColumnElement[Any]
    fields: dict[str, LegacyField]
    # Row-level read policy: a WHERE clause for this user (sqlalchemy.false() = nothing).
    read_policy: Callable[[CurrentUser], ColumnElement[bool]]
    # Rows that exist as this entity at all (e.g. users that have a customer profile).
    base_where: ColumnElement[bool] = field(default_factory=true)
    created_by_expr: ColumnElement[Any] | None = None
    create: CreateHook | None = None
    update: UpdateHook | None = None
    delete: DeleteHook | None = None


REGISTRY: dict[str, EntityDef] = {}


def register(entity: EntityDef) -> EntityDef:
    if entity.name in REGISTRY:
        raise RuntimeError(f"entity {entity.name} registered twice")
    REGISTRY[entity.name] = entity
    return entity


def get_entity(name: str) -> EntityDef | None:
    return REGISTRY.get(name)
