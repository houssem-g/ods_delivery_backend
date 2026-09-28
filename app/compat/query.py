"""Reading legacy documents: filter translation, sort, pagination, read policy, serialization.

The read policy and field guards are part of the SQL, so LIMIT never applies before
the policy (a Base44 client-side filter after LIMIT would lose rows).
"""

import json
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    and_,
    case,
    cast,
    false,
    not_,
    null,
    or_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.registry import EntityDef, FieldType, LegacyField
from app.compat.values import NO_MATCH, ValueError400, to_json, to_sql
from app.security.deps import CurrentUser

MAX_LIMIT = 1000
DEFAULT_LIMIT = MAX_LIMIT  # the SDK sends no limit on some admin lists
BUILTIN_FIELDS = ("id", "created_date", "updated_date", "created_by")
_COMPARISONS = {"$gt": "__gt__", "$gte": "__ge__", "$lt": "__lt__", "$lte": "__le__"}
_OPERATORS = {"$in", "$nin", "$ne", "$exists", *_COMPARISONS}


class QueryError(Exception):
    """A bad filter / sort / pagination: answered 400 with this message."""


def _builtin(entity: EntityDef, name: str) -> LegacyField:
    exprs: dict[str, tuple[ColumnElement[Any], FieldType]] = {
        "id": (entity.id_expr, "id" if entity.id_type == "uuid" else "string"),
        "created_date": (entity.created_expr, "datetime"),
        "updated_date": (entity.updated_expr, "datetime"),
        "created_by": (
            entity.created_by_expr if entity.created_by_expr is not None else null(),
            "string",
        ),
    }
    expr, type_ = exprs[name]
    return LegacyField(expr=expr, type=type_)


def resolve_field(entity: EntityDef, name: str) -> LegacyField:
    if name in BUILTIN_FIELDS:
        return _builtin(entity, name)
    spec = entity.fields.get(name)
    if spec is None:
        raise QueryError(f"Unknown field '{name}' for entity {entity.name}")
    return spec


def visible_expr(spec: LegacyField, user: CurrentUser) -> ColumnElement[Any]:
    """The field as this user may see it: NULL where its guard is false."""
    if spec.read_guard is None:
        return spec.expr
    return case((spec.read_guard(user), spec.expr), else_=null())


def _coerce(value: Any, spec: LegacyField, name: str) -> Any:
    if spec.type in ("object", "array"):
        raise QueryError(f"Field '{name}' only supports $exists and null comparisons")
    try:
        return to_sql(value, spec.type, name)
    except ValueError400 as exc:
        raise QueryError(str(exc)) from exc


def _equals(expr: ColumnElement[Any], spec: LegacyField, name: str, value: Any) -> ColumnElement[bool]:
    if value is None:
        return expr.is_(None)
    coerced = _coerce(value, spec, name)
    return false() if coerced is NO_MATCH else expr == coerced


def _in_list(
    expr: ColumnElement[Any], spec: LegacyField, name: str, values: Any, op: str
) -> ColumnElement[bool]:
    if not isinstance(values, list):
        raise QueryError(f"{op} on '{name}' expects an array")
    coerced = [_coerce(v, spec, name) for v in values if v is not None]
    usable = [v for v in coerced if v is not NO_MATCH]
    wants_null = any(v is None for v in values)
    positive = or_(expr.in_(usable) if usable else false(), expr.is_(None) if wants_null else false())
    if op == "$in":
        return positive
    # Mongo semantics: $nin also matches documents without the field (unless null is listed).
    return or_(not_(positive), false() if wants_null else expr.is_(None))


def _condition(entity: EntityDef, user: CurrentUser, name: str, value: Any) -> ColumnElement[bool]:
    spec = resolve_field(entity, name)
    expr = visible_expr(spec, user)
    if not isinstance(value, dict):
        if isinstance(value, list):
            raise QueryError(f"Use $in to match '{name}' against several values")
        return _equals(expr, spec, name, value)
    if not value:
        raise QueryError(f"Empty operator object for '{name}'")
    clauses: list[ColumnElement[bool]] = []
    for op, operand in value.items():
        if op not in _OPERATORS:
            raise QueryError(f"Unsupported operator '{op}'")
        if op in ("$in", "$nin"):
            clauses.append(_in_list(expr, spec, name, operand, op))
        elif op == "$ne":
            if operand is None:
                clauses.append(expr.is_not(None))
            else:
                coerced = _coerce(operand, spec, name)
                clauses.append(true() if coerced is NO_MATCH else expr.is_distinct_from(coerced))
        elif op == "$exists":
            if not isinstance(operand, bool):
                raise QueryError(f"$exists on '{name}' expects true or false")
            clauses.append(expr.is_not(None) if operand else expr.is_(None))
        else:
            if operand is None:
                raise QueryError(f"{op} on '{name}' needs a value")
            coerced = _coerce(operand, spec, name)
            clauses.append(false() if coerced is NO_MATCH else getattr(expr, _COMPARISONS[op])(coerced))
    return and_(*clauses)


def translate_filter(entity: EntityDef, user: CurrentUser, query: dict[str, Any]) -> ColumnElement[bool]:
    if not isinstance(query, dict):
        raise QueryError("The filter must be a JSON object")
    return and_(true(), *(_condition(entity, user, name, value) for name, value in query.items()))


def parse_query_param(raw: str | None) -> dict[str, Any]:
    if raw is None or raw.strip() == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QueryError("q is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise QueryError("The filter must be a JSON object")
    return parsed


def _order_by(entity: EntityDef, user: CurrentUser, sort: str | None) -> list[Any]:
    tie_breaker = entity.id_expr.desc()
    if not sort:
        return [entity.created_expr.desc(), tie_breaker]
    descending = sort.startswith("-")
    name = sort.lstrip("+-")
    spec = resolve_field(entity, name)
    if spec.type in ("object", "array"):
        raise QueryError(f"Cannot sort on '{name}'")
    expr = visible_expr(spec, user)
    return [expr.desc().nulls_last() if descending else expr.asc().nulls_last(), tie_breaker]


def _columns(entity: EntityDef, user: CurrentUser) -> list[Any]:
    cols: list[Any] = [
        entity.id_expr.label("id"),
        entity.created_expr.label("created_date"),
        entity.updated_expr.label("updated_date"),
        (entity.created_by_expr if entity.created_by_expr is not None else cast(null(), String)).label(
            "created_by"
        ),
    ]
    cols += [visible_expr(spec, user).label(name) for name, spec in entity.fields.items()]
    return cols


def base_select(entity: EntityDef, user: CurrentUser) -> Select[Any]:
    return (
        select(*_columns(entity, user))
        .select_from(entity.source)
        .where(entity.base_where, entity.read_policy(user))
    )


def serialize(entity: EntityDef, row: Any) -> dict[str, Any]:
    mapping = row._mapping
    doc: dict[str, Any] = {
        "id": to_json(mapping["id"], "string"),
        "created_date": to_json(mapping["created_date"], "datetime"),
        "updated_date": to_json(mapping["updated_date"], "datetime"),
        "created_by": mapping["created_by"],
    }
    for name, spec in entity.fields.items():
        doc[name] = to_json(mapping[name], spec.type)
    return doc


async def list_documents(
    session: AsyncSession,
    entity: EntityDef,
    user: CurrentUser,
    query: dict[str, Any],
    sort: str | None = None,
    limit: int | None = None,
    skip: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None and limit < 0:
        raise QueryError("limit must be >= 0")
    if skip is not None and skip < 0:
        raise QueryError("skip must be >= 0")
    stmt = (
        base_select(entity, user)
        .where(translate_filter(entity, user, query))
        .order_by(*_order_by(entity, user, sort))
        .limit(min(limit or DEFAULT_LIMIT, MAX_LIMIT))
        .offset(skip or 0)
    )
    rows = (await session.execute(stmt)).all()
    return [serialize(entity, row) for row in rows]


def id_condition(entity: EntityDef, raw_id: str) -> ColumnElement[bool]:
    value = to_sql(raw_id, "id" if entity.id_type == "uuid" else "string", "id")
    return false() if value is NO_MATCH else entity.id_expr == value


async def get_document(
    session: AsyncSession, entity: EntityDef, user: CurrentUser, raw_id: str
) -> dict[str, Any] | None:
    stmt = base_select(entity, user).where(id_condition(entity, raw_id))
    row = (await session.execute(stmt)).first()
    return serialize(entity, row) if row is not None else None
