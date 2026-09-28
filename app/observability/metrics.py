"""Prometheus metrics, served on GET /api/metrics.

Off unless METRICS_TOKEN is set (the route answers 404); callers send the token in
`X-Metrics-Token` (or `Authorization: Bearer`). One registry per process: run one
uvicorn worker per pod (WEB_CONCURRENCY=1) and let Prometheus scrape every pod.
"""

import hmac
from collections.abc import Iterator

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.config import settings
from app.errors import ApiError

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

HTTP_REQUESTS = Counter(
    "odsd_http_requests_total",
    "HTTP requests by route template and status",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "odsd_http_request_duration_seconds",
    "HTTP request duration by route template",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
WS_CONNECTIONS = Gauge("odsd_ws_connections", "Open realtime WebSocket connections", registry=REGISTRY)
JOB_RUNS = Counter("odsd_job_runs_total", "Scheduled job runs", ["job"], registry=REGISTRY)
JOB_FAILURES = Counter("odsd_job_failures_total", "Job runs that raised", ["job"], registry=REGISTRY)
JOB_DURATION = Histogram(
    "odsd_job_duration_seconds",
    "Scheduled job duration",
    ["job"],
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900),
    registry=REGISTRY,
)
SCHEDULER_LEADER = Gauge("odsd_scheduler_leader", "1 when this process runs the jobs", registry=REGISTRY)


def _ws_connections() -> int:
    from app.realtime.hub import hub

    return len(hub.subscribers)


WS_CONNECTIONS.set_function(_ws_connections)


class DbPoolCollector(Collector):
    """SQLAlchemy pool of this process (nothing with NullPool)."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        from app.db import engine

        pool = engine.pool
        if not hasattr(pool, "checkedout"):
            return
        for name, doc, value in (
            ("odsd_db_pool_size", "Configured pool size", pool.size()),
            ("odsd_db_pool_checked_out", "Connections in use", pool.checkedout()),
            ("odsd_db_pool_checked_in", "Idle connections in the pool", pool.checkedin()),
            ("odsd_db_pool_overflow", "Connections beyond pool_size (<0: not opened yet)", pool.overflow()),
        ):
            yield GaugeMetricFamily(name, doc, value=value)


REGISTRY.register(DbPoolCollector())


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def observe_job(name: str, ok: bool, seconds: float) -> None:
    JOB_RUNS.labels(name).inc()
    if not ok:
        JOB_FAILURES.labels(name).inc()
    JOB_DURATION.labels(name).observe(seconds)


router = APIRouter(tags=["metrics"])


def _presented_token(request: Request) -> str:
    token = request.headers.get("x-metrics-token", "")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    return token


@router.get("/api/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    expected = settings.METRICS_TOKEN
    if not expected:
        raise ApiError(404, "not_found", "Not Found")
    if not hmac.compare_digest(_presented_token(request).encode(), expected.encode()):
        raise ApiError(401, "unauthorized", "Invalid metrics token")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
