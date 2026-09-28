"""Converting values between the legacy JSON shape and SQL, per legacy field type."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from app.compat.dates import legacy_datetime, parse_legacy_datetime
from app.compat.registry import FieldType


class ValueError400(Exception):
    """A value that does not fit the field: answered 400."""


class NoMatch:
    """A filter value that can never match (e.g. a non-uuid string compared with a uuid column)."""


NO_MATCH = NoMatch()


def to_sql(value: Any, type_: FieldType, name: str) -> Any:
    """Legacy JSON value -> Python value for a comparison or a write. None passes through."""
    if value is None:
        return None
    if type_ == "id":
        if not isinstance(value, str):
            raise ValueError400(f"{name}: expected an id string")
        try:
            return uuid.UUID(value)
        except ValueError:
            return NO_MATCH
    if type_ == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError400(f"{name}: expected a string")
        return str(value)
    if type_ in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError400(f"{name}: expected a number")
        if type_ == "integer" and not float(value).is_integer():
            raise ValueError400(f"{name}: expected an integer")
        return Decimal(str(value)) if type_ == "number" else int(value)
    if type_ == "boolean":
        if not isinstance(value, bool):
            raise ValueError400(f"{name}: expected true or false")
        return value
    if type_ == "datetime":
        if not isinstance(value, str):
            raise ValueError400(f"{name}: expected an ISO date-time string")
        try:
            return parse_legacy_datetime(value)
        except ValueError as exc:
            raise ValueError400(f"{name}: invalid date-time") from exc
    if type_ == "object":
        if not isinstance(value, dict):
            raise ValueError400(f"{name}: expected an object")
        return value
    if type_ == "array":
        if not isinstance(value, list):
            raise ValueError400(f"{name}: expected an array")
        return value
    raise ValueError400(f"{name}: unsupported type")


def to_json(value: Any, type_: FieldType) -> Any:
    """SQL value -> legacy JSON value."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return legacy_datetime(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if type_ == "integer" else float(value)
    return value
