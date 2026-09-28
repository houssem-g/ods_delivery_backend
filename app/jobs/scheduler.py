"""APScheduler in the API process; exactly one leader per database.

Leadership = a session-level `pg_try_advisory_lock` held on a dedicated asyncpg
connection (the ods-be proactive_engine pattern). Followers retry periodically, so
when the leader's process or connection dies another process takes over.
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.db import asyncpg_dsn
from app.jobs.registry import JOBS, Job

log = logging.getLogger("odsd.jobs")
PING_SECONDS = 30


async def run_job(job: Job) -> dict[str, Any]:
    """Runs one job, logging its outcome; never raises (the scheduler must keep going)."""
    started = time.monotonic()
    try:
        result = await job.func()
    except Exception as exc:
        log.exception("job %s failed", job.name)
        return {"job": job.name, "ok": False, "error": type(exc).__name__}
    elapsed_ms = round((time.monotonic() - started) * 1000)
    log.info("job %s done in %d ms: %s", job.name, elapsed_ms, result)
    return {"job": job.name, "ok": True, "duration_ms": elapsed_ms, "result": result or {}}


async def try_lock(dsn: str, key: int) -> asyncpg.Connection | None:
    """A connection holding the advisory lock, or None if another process holds it."""
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        if await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
            return conn
    except Exception:
        await conn.close()
        raise
    await conn.close()
    return None


class LeaderScheduler:
    def __init__(self, jobs: dict[str, Job] | None = None, lock_key: int | None = None) -> None:
        self.jobs = JOBS if jobs is None else jobs
        self.lock_key = settings.SCHEDULER_LOCK_KEY if lock_key is None else lock_key
        self.is_leader = False
        self._task: asyncio.Task[None] | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._conn: asyncpg.Connection | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="scheduler-leader")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._step_down()

    async def _run(self) -> None:
        while True:
            try:
                self._conn = await try_lock(asyncpg_dsn(settings), self.lock_key)
                if self._conn is not None:
                    self._lead()
                    await self._hold()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("scheduler leadership error: %s", exc)
            await self._step_down()
            await asyncio.sleep(settings.SCHEDULER_LEADER_RETRY_SECONDS)

    def _lead(self) -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        for job in self.jobs.values():
            if job.enabled:
                scheduler.add_job(
                    run_job, job.trigger, args=[job], id=job.name, max_instances=1, coalesce=True,
                    replace_existing=True,
                )  # fmt: skip
        scheduler.start()
        self._scheduler = scheduler
        self.is_leader = True
        log.info("scheduler leader: %d jobs scheduled", len(scheduler.get_jobs()))

    async def _hold(self) -> None:
        """Keeps the lock; returns when the lock connection stops answering."""
        assert self._conn is not None
        while True:
            await asyncio.sleep(PING_SECONDS)
            await asyncio.wait_for(self._conn.execute("SELECT 1"), timeout=10)

    async def _step_down(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self.is_leader = False
        if self._conn is not None:
            # Closing the session releases the advisory lock.
            with contextlib.suppress(Exception):
                await self._conn.close(timeout=2)
            self._conn = None
