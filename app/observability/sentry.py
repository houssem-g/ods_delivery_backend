"""Optional Sentry. SENTRY_DSN empty = off (nothing imported at start-up, nothing sent).

send_default_pii is off, request bodies are never sent, and every event passes through
`scrub_event`: sensitive headers and cookies dropped, strings redacted, user reduced to a hash.
"""

import logging
from typing import Any

from app.config import Settings
from app.observability.context import request_id_var, user_hash
from app.observability.redact import SENSITIVE_HEADERS, redact, redact_value

log = logging.getLogger("odsd.sentry")


def scrub_event(event: dict[str, Any], key: str) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        request.pop("data", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: "[redacted]" if k.lower() in SENSITIVE_HEADERS else redact(str(v))
                for k, v in headers.items()
            }
        if isinstance(request.get("query_string"), str):
            request["query_string"] = redact(request["query_string"])
        if isinstance(request.get("url"), str):
            request["url"] = redact(request["url"])
    user = event.get("user")
    if isinstance(user, dict):
        uid = user.get("id")
        event["user"] = {"id": user_hash(str(uid), key)} if uid else {}
    for field in ("exception", "logentry", "message", "breadcrumbs", "extra", "contexts"):
        if field in event:
            event[field] = redact_value(event[field])
    rid = request_id_var.get()
    if rid:
        event.setdefault("tags", {})["request_id"] = rid
    return event


def init_sentry(cfg: Settings) -> bool:
    if not cfg.SENTRY_DSN:
        return False
    import sentry_sdk

    def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
        return scrub_event(event, cfg.JWT_SECRET)

    def before_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
        return redact_value(crumb)

    sentry_sdk.init(
        dsn=cfg.SENTRY_DSN,
        environment=cfg.SENTRY_ENVIRONMENT or cfg.ENVIRONMENT,
        release=cfg.APP_RELEASE or None,
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=cfg.SENTRY_TRACES_SAMPLE_RATE,
        before_send=before_send,  # type: ignore[arg-type]
        before_breadcrumb=before_breadcrumb,  # type: ignore[arg-type]
    )
    log.info("sentry enabled (environment %s)", cfg.SENTRY_ENVIRONMENT or cfg.ENVIRONMENT)
    return True
