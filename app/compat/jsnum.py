"""JavaScript number coercions used by the legacy functions (`Number(x)`, `parseInt`,
`parseFloat`, `x || default`), so the same payloads give the same bounds and defaults."""

import math
import re
from typing import Any

_INT_PREFIX = re.compile(r"^\s*([+-]?\d+)")
_FLOAT_PREFIX = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def is_finite_number(value: Any) -> bool:
    """`Number.isFinite(value)`: true only for an actual finite number (never a string)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def js_number(value: Any) -> float:
    """`Number(value)` for JSON values: null → 0, '' → 0, bad string → NaN."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        try:
            parsed = float(text)
        except ValueError:
            return math.nan
        return parsed if text.lower().lstrip("+-") not in ("inf", "infinity", "nan") else math.nan
    return math.nan


def truthy(value: float) -> bool:
    return not (value == 0 or math.isnan(value))


def number_or(value: Any, default: float) -> float:
    """`Number(value) || default`."""
    number = js_number(value)
    return number if truthy(number) else default


def parse_int(value: Any) -> float:
    """`parseInt(String(value), 10)`: NaN when no leading digits."""
    match = _INT_PREFIX.match("" if value is None else str(value))
    return float(int(match.group(1))) if match else math.nan


def parse_float(value: Any) -> float:
    """`parseFloat(String(value))`."""
    match = _FLOAT_PREFIX.match("" if value is None else str(value))
    return float(match.group(1)) if match else math.nan


def clamp(value: float, low: float, high: float) -> float:
    """`Math.min(high, Math.max(low, value))` (NaN stays NaN, like JS)."""
    if math.isnan(value):
        return value
    return min(high, max(low, value))
