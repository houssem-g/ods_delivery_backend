"""Order jobs: stale order expiry, orphan offers, expired drafts, stock-check timeout, courier
presence, weekly commission statements."""

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db import transaction
from app.jobs.registry import job
from app.services import commission, couriers, expiry, offer_intents, order_drafts, stock_checks

log = logging.getLogger("odsd.jobs")


@job(
    "expire_stale_orders",
    IntervalTrigger(minutes=5),
    "open orders idle 24 h and deliveries idle 48 h → cancelled (expireStaleOrders)",
)
async def expire_stale_orders() -> dict[str, Any]:
    async with transaction() as session:
        return await expiry.expire_stale_orders(session)


@job(
    "expire_orphan_offers",
    IntervalTrigger(hours=1),
    "pending offers of closed orders → expired; offer intents older than 10 min → deleted",
)
async def expire_orphan_offers() -> dict[str, Any]:
    async with transaction() as session:
        closed = await expiry.expire_orphan_offers(session)
        intents = await offer_intents.purge_stale(session)
    return {"offers_closed": closed, "offer_intents_deleted": intents}


@job("purge_order_drafts", IntervalTrigger(hours=1), "order drafts past their 24 h → deleted")
async def purge_order_drafts() -> dict[str, Any]:
    async with transaction() as session:
        return {"drafts_deleted": await order_drafts.purge_expired(session)}


@job(
    "stock_check_timeout",
    IntervalTrigger(minutes=1),
    "unanswered stock checks past their 5 minutes → the order's unavailable_policy applies",
)
async def stock_check_timeout() -> dict[str, Any]:
    async with transaction() as session:
        due = await stock_checks.due_order_ids(session)
    advanced = errors = 0
    for order_id in due:
        try:
            async with transaction() as session:
                advanced += await stock_checks.sweep_order(session, order_id)
        except Exception:  # one bad order never blocks the others
            errors += 1
            log.exception("stock_check_timeout: order %s failed", order_id)
    return {"checked": len(due), "advanced": advanced, "errors": errors}


@job("courier_presence_expiry", IntervalTrigger(minutes=5), "online couriers silent for 15 min → offline")
async def courier_presence_expiry() -> dict[str, Any]:
    async with transaction() as session:
        return {"set_offline": await couriers.expire_presence(session)}


async def build_commission_statements() -> dict[str, Any]:
    async with transaction() as session:
        return await commission.build_statements(session)


# Monday 04:00 in Tunis (the scheduler itself runs in UTC).
STATEMENTS_TRIGGER = CronTrigger(day_of_week="mon", hour=4, minute=0, timezone="Africa/Tunis")
