"""Messaging jobs: WhatsApp/SMS pending checks (5 min) and the hourly test-data purge.

- `whatsapp_check_pending`: port of sendWhatsAppMessage `check_pending` (sweepNoResponse
  called it every 5 min on Base44): SMS for critical WhatsApp messages not delivered
  within WHATSAPP_FALLBACK_SECONDS, retries of transient failures.
- `test_data_purge`: port of sweepExpiredTestData. Its only work on Base44 was on
  ResaleOrder (expired unsold hot deals, `test:` deals), which the hot-deals port owns:
  it plugs its step in with `@purge_step("hot_deals")` (HOOK below). Each step runs in
  its own transaction and returns counters merged into the summary (Deno metric names:
  `expired_deals_deleted`, `test_run_deals_deleted`); a failing step is counted in
  `failed_steps` and does not stop the others.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import transaction
from app.jobs.registry import job
from app.security.tokens import now_utc
from app.services import whatsapp

log = logging.getLogger("odsd.jobs")

PurgeStep = Callable[[AsyncSession], Awaitable[dict[str, int]]]
PURGE_STEPS: dict[str, PurgeStep] = {}


def purge_step(name: str) -> Callable[[PurgeStep], PurgeStep]:
    """HOOK for test_data_purge: register `async def step(session) -> {counter: n}`."""

    def register(func: PurgeStep) -> PurgeStep:
        if name in PURGE_STEPS:
            raise RuntimeError(f"purge step {name} registered twice")
        PURGE_STEPS[name] = func
        return func

    return register


@job(
    "whatsapp_check_pending",
    IntervalTrigger(minutes=5),
    "WhatsApp/SMS pending checks (SMS fallback, retries)",
)
async def whatsapp_check_pending() -> dict[str, Any]:
    async with transaction() as session:
        _status, body = await whatsapp.check_pending(session)
    return {"fallbacks": body["fallbacks"], "retried": body["retried"]}


@job("test_data_purge", IntervalTrigger(hours=1), "test data purge (sweepExpiredTestData)")
async def test_data_purge() -> dict[str, Any]:
    started = time.monotonic()
    metrics: dict[str, Any] = {
        "timestamp": now_utc().isoformat(),
        "steps": sorted(PURGE_STEPS),
        "failed_steps": 0,
    }
    for name, step in PURGE_STEPS.items():
        try:
            async with transaction() as session:
                counters = await step(session)
        except Exception:
            log.exception("test_data_purge step %s failed", name)
            metrics["failed_steps"] += 1
            continue
        for key, value in (counters or {}).items():
            metrics[key] = metrics.get(key, 0) + value
    metrics["total_runtime_ms"] = round((time.monotonic() - started) * 1000)
    return metrics
