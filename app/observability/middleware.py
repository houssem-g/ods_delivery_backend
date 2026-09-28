"""Pure ASGI middleware (HTTP and WebSocket): request id in and out, one access-log line and
the HTTP metrics per request. Outermost middleware, so CORS answers and 429s are counted.

The access line holds: method, route template (never the raw path or query string), status,
duration and a keyed hash of the user id. No token, e-mail or body is ever logged.
"""

import logging
import time
import uuid
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.observability import metrics
from app.observability.context import accept_request_id, request_id_var, user_hash
from app.security.tokens import InvalidToken, decode_access_token

access_log = logging.getLogger("odsd.access")
REQUEST_ID_HEADER = "X-Request-ID"
UNMATCHED = "<unmatched>"
# Health probes and scrapes every few seconds: counted, logged only at DEBUG.
QUIET_ROUTES = frozenset({"/api/health", "/api/health/ready", "/api/metrics"})


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED


def _user(scope: Scope, headers: Headers) -> str | None:
    token = None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif scope["type"] == "websocket":
        token = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token") or [None])[0]
    if not token:
        return None
    try:
        return user_hash(str(decode_access_token(token)["sub"]), settings.JWT_SECRET)
    except (InvalidToken, KeyError):
        return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = accept_request_id(headers.get("x-request-id")) or uuid.uuid4().hex
        reset = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500 if scope["type"] == "http" else 101

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            elif message["type"] == "websocket.accept":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            elif message["type"] == "websocket.close" and status == 101:
                status = 403  # closed before being accepted
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            method = scope.get("method", "WS") if scope["type"] == "http" else "WS"
            if scope["type"] == "http":
                metrics.observe_request(method, route, status, elapsed)
            level = logging.DEBUG if route in QUIET_ROUTES and status < 400 else logging.INFO
            if access_log.isEnabledFor(level):
                access_log.log(
                    level,
                    "%s %s %s %.1fms",
                    method,
                    route,
                    status,
                    elapsed * 1000,
                    extra={
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 1),
                        "user": _user(scope, headers),
                    },
                )
            request_id_var.reset(reset)
