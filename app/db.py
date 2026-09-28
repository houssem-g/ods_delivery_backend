"""Async engine, session dependency and the `transaction()` helper.

Pool sizing follows the ods-be budget rule: every process gets at most
(DB_MAX_CONNECTIONS - DB_CONNECTION_RESERVE) / (WEB_CONCURRENCY x DEPLOYMENT_REPLICAS)
connections, so scaling out can never exhaust Postgres. Two more connections per
process live outside the pool: the realtime LISTEN connection and the scheduler
leader lock; keep them inside the reserve.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, settings

log = logging.getLogger("odsd.db")


def resolve_pool(cfg: Settings) -> dict[str, Any]:
    """Per-process pool kwargs clamped to the deployment-wide connection budget."""
    if cfg.DB_NULLPOOL:
        return {"poolclass": NullPool}
    concurrency = max(1, cfg.WEB_CONCURRENCY) * max(1, cfg.DEPLOYMENT_REPLICAS)
    budget = max(1, cfg.DB_MAX_CONNECTIONS - cfg.DB_CONNECTION_RESERVE)
    cap = max(1, budget // concurrency)
    pool_size = max(1, min(cfg.DB_POOL_SIZE, cap))
    max_overflow = max(0, min(cfg.DB_MAX_OVERFLOW, cap - pool_size))
    if budget // concurrency < 1:
        log.warning("DB pool over budget: %d processes for %d connections", concurrency, budget)
    return {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": cfg.DB_POOL_TIMEOUT,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }


def connect_args(cfg: Settings) -> dict[str, Any]:
    # JIT only slows our short OLTP statements (asyncpg's type introspection
    # query took >1 s per fresh connection in ods-be with JIT on).
    args: dict[str, Any] = {"server_settings": {"jit": "off", "application_name": "ods-delivery-api"}}
    if cfg.DB_SSLMODE == "require":
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        args["ssl"] = ctx
    else:
        args["ssl"] = False
    return args


def asyncpg_dsn(cfg: Settings) -> str:
    """DSN for raw asyncpg connections (realtime listener, leader lock)."""
    return cfg.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL, connect_args=connect_args(settings), **resolve_pool(settings)
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Handlers commit explicitly; anything uncommitted is rolled back."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """A session inside one transaction: committed on success, rolled back on error.

    For jobs, scripts and services running outside a request.
    """
    async with SessionLocal() as session, session.begin():
        yield session
