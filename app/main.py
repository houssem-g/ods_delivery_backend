"""App factory. The lifespan checks the database, initialises Firebase when credentials exist,
and runs the realtime listener and the (leader-elected) scheduler."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from sqlalchemy import text

from app.api import admin, auth, compat_entities, compat_functions, files, health, webhooks, ws
from app.compat import entities as _registered_entities  # noqa: F401 - import registers them
from app.config import settings
from app.db import engine
from app.errors import install_error_handlers
from app.jobs import incidents as _incident_jobs  # noqa: F401 - import registers them
from app.jobs import messaging as _messaging_jobs  # noqa: F401 - import registers them
from app.jobs import orders as _order_jobs  # noqa: F401 - import registers them
from app.jobs import periodic as _periodic_jobs  # noqa: F401 - import registers them
from app.jobs.scheduler import LeaderScheduler
from app.observability import metrics
from app.observability.logs import configure_logging
from app.observability.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.observability.sentry import init_sentry
from app.rate_limit import limiter, rate_limit_exceeded
from app.realtime.hub import hub
from app.realtime.listener import PgListener
from app.services.push import init_firebase

log = logging.getLogger("odsd")


async def _check_database() -> None:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        # Keep starting: readiness answers 503 until the database is back.
        log.error("database unreachable at start-up: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    await _check_database()
    init_firebase()
    listener = PgListener(hub) if settings.REALTIME_ENABLED else None
    scheduler = LeaderScheduler() if settings.SCHEDULER_ENABLED else None
    if listener is not None:
        hub.start()
        listener.start()
    if scheduler is not None:
        scheduler.start()
    app.state.listener, app.state.scheduler = listener, scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        if listener is not None:
            await listener.stop()
            await hub.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    init_sentry(settings)
    application = FastAPI(
        title="ODS Delivery API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.is_local else None,
    )
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, rate_limit_exceeded)  # type: ignore[arg-type]
    install_error_handlers(application)
    application.add_middleware(SlowAPIASGIMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "x-cron-token", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Outermost: request id, access log and HTTP metrics cover CORS answers and 429s too.
    application.add_middleware(RequestContextMiddleware)
    for router in (
        health.router,
        auth.router,
        files.router,
        compat_entities.router,
        webhooks.router,  # before compat_functions: it owns /api/functions/whatsappWebhook
        compat_functions.router,
        admin.router,
        ws.router,
        metrics.router,
    ):
        application.include_router(router)
    limiter.exempt(health.liveness)
    limiter.exempt(health.readiness)
    limiter.exempt(metrics.metrics)
    return application


app = create_app()
