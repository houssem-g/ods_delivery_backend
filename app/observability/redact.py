"""Scrubs secrets and personal data out of anything that is logged or sent to Sentry.

Never logged: bearer / JWT tokens, token-like query parameters and fields, passwords,
e-mail addresses, phone numbers.
"""

import re
from typing import Any

_SECRET_KEYS = (
    r"access_token|refresh_token|id_token|token|password|new_password|old_password|passwd|secret"
    r"|client_secret|api_key|apikey|code|otp|authorization|cookie|x-cron-token|x-metrics-token"
)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[jwt]"),
    # key=value (query strings, form bodies) and "key": "value" (JSON, reprs)
    (re.compile(rf"(?i)\b({_SECRET_KEYS})=([^&\s\"',;]+)"), r"\1=[redacted]"),
    (
        re.compile(rf"(?i)(?<![\w-])([\"']?(?:{_SECRET_KEYS})[\"']?\s*:\s*)(\"[^\"]*\"|'[^']*'|[^,\s}}]+)"),
        r"\1[redacted]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    # international (+216…, 00216…) and 8-digit Tunisian numbers
    (re.compile(r"(?<![\w.+-])(?:\+|00)\d(?:[ -]?\d){7,14}(?!\w)"), "[phone]"),
    (re.compile(r"(?<![\w.:-])\d{8}(?![\w.:-])"), "[phone]"),
]
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "x-cron-token", "x-metrics-token"})


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redacts strings inside dicts / lists (Sentry events)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: "[redacted]" if _is_secret_key(k) else redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def _is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and re.fullmatch(rf"(?i){_SECRET_KEYS}", key) is not None
