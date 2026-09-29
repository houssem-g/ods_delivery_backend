"""Incident jobs: the "client ne répond pas" sweep (5 min) and the hot-deal expiry (hourly),
plus the "hot_deals" step of the hourly test-data purge (app/jobs/messaging.py).

- `sweep_5min` (port of sweepNoResponse → triggerEmergencyContact {action:'sweep'}): every order
  still in client_no_response (and every order that left it with a case still open) is
  brought up to date: incident + "last chance" alerts once the deadline is past, automatic
  closing AUTO_CLOSE_HOURS after it. One transaction per order, SKIP LOCKED: an order a user is
  acting on is looked at again at the next run. The rest of sweepNoResponse runs as its own
  jobs: whatsapp_check_pending, expire_stale_orders, expire_orphan_offers, test_data_purge.
- `no_response_fast` (15 s): the same step for the cases still counting down, so the customer's
  reminders (every 45 s) and the "last chance" alert leave on time, not up to 5 min late.
- `hourly_cleanup`: listed hot deals past their expiry → expired (announced to the listings).
- purge step `hot_deals` (sweepExpiredTestData): expired unsold deals and expired QA deals are
  deleted → {expired_deals_deleted, test_run_deals_deleted}.
All idempotent: they only do work that is already due.
"""

import logging
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import transaction
from app.jobs.messaging import purge_step
from app.jobs.registry import job
from app.services import hot_deals, no_response

log = logging.getLogger("odsd.jobs")


@job("sweep_5min", IntervalTrigger(minutes=5), "no-response sweep (triggerEmergencyContact sweep)")
async def sweep_5min() -> dict[str, Any]:
    async with transaction() as session:
        due = await no_response.due_order_ids(session)
    advanced = errors = 0
    for order_id in due:
        try:
            async with transaction() as session:
                if await no_response.sweep_order(session, order_id):
                    advanced += 1
        except Exception:  # one bad order never blocks the others
            errors += 1
            log.exception("sweep_5min: order %s failed", order_id)
    return {"success": True, "checked": len(due), "advanced": advanced, "errors": errors}


@job("no_response_fast", IntervalTrigger(seconds=15), "no-response reminders and deadline (waiting cases)")
async def no_response_fast() -> dict[str, Any] | None:
    async with transaction() as session:
        due = await no_response.waiting_order_ids(session)
    if not due:
        return None
    advanced = errors = 0
    for order_id in due:
        try:
            async with transaction() as session:
                if await no_response.sweep_order(session, order_id):
                    advanced += 1
        except Exception:
            errors += 1
            log.exception("no_response_fast: order %s failed", order_id)
    return {"checked": len(due), "advanced": advanced, "errors": errors}


@job("hourly_cleanup", IntervalTrigger(hours=1), "expired hot deals (listed deals past their expiry)")
async def hourly_cleanup() -> dict[str, Any]:
    async with transaction() as session:
        expired = await hot_deals.expire_deals(session)
    return {"expired_deals": expired}


@purge_step("hot_deals")
async def purge_hot_deals(session: AsyncSession) -> dict[str, int]:
    return await hot_deals.purge_deals(session)
