"""Per-request context shared by the middleware, the log records and Sentry events."""

import hashlib
import hmac
import re
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# An incoming X-Request-ID is kept only when it is short and harmless (it is logged and echoed).
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def accept_request_id(value: str | None) -> str | None:
    if value and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    return None


def user_hash(user_id: str, key: str) -> str:
    """Stable, non-reversible id for logs: correlates a user's requests without naming them."""
    return hmac.new(key.encode(), f"log-user:{user_id}".encode(), hashlib.sha256).hexdigest()[:16]
