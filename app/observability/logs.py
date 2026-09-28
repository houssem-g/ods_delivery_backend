"""Logging set-up: LOG_FORMAT=text (default, humans) or json (one object per line, for Loki).

Every record goes through the redaction of `redact.py` and carries the request id.
uvicorn's own access log is turned off: it prints query strings (the WebSocket token);
the RequestContextMiddleware writes one redacted line per request instead.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.observability.context import request_id_var
from app.observability.redact import redact

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = getattr(record, "request_id", None)
        return redact(f"{line} [rid={rid}]" if rid else line)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_") and value is not None:
                entry[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RequestIdFilter())
    handler.set_name("odsd")
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "odsd" and _keep(h)]
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def _keep(handler: logging.Handler) -> bool:
    # pytest's capture handlers stay; any plain stream handler (basicConfig, uvicorn) goes.
    return type(handler).__module__.startswith("_pytest")
