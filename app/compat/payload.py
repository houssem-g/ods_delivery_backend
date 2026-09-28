"""Validating the body of a legacy entity write before a write hook uses it."""

from typing import Any

from app.compat.registry import EntityDef
from app.compat.values import NO_MATCH, ValueError400, to_sql
from app.errors import ApiError


def coerce_payload(entity: EntityDef, data: Any, allowed: set[str] | frozenset[str]) -> dict[str, Any]:
    """Allowed legacy fields, converted to Python values (Decimal, datetime, UUID...).

    Unknown or non-writable keys are ignored, as Base44 ignored them: the front
    spreads whole objects (id, created_date...) into its updates. A writable field
    with a value of the wrong type is a 400.
    """
    if not isinstance(data, dict):
        raise ApiError(400, "validation_error", "The body must be a JSON object")
    clean: dict[str, Any] = {}
    for name, value in data.items():
        if name not in allowed or name not in entity.fields:
            continue
        try:
            converted = to_sql(value, entity.fields[name].type, name)
        except ValueError400 as exc:
            raise ApiError(400, "validation_error", str(exc)) from exc
        if converted is NO_MATCH:
            raise ApiError(400, "validation_error", f"{name}: invalid id")
        clean[name] = converted
    return clean
