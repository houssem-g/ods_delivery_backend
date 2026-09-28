"""Order jobs: stale order expiry, orphan offers, courier presence, weekly commission statements."""

from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db import transaction
from app.jobs.registry import job
from app.services import commission, couriers, expiry


@job(
    "expire_stale_orders",
    IntervalTrigger(minutes=5),
    "open orders idle 24 h and deliveries idle 48 h → cancelled (expireStaleOrders)",
)
async def expire_stale_orders() -> dict[str, Any]:
    async with transaction() as session:
        return await expiry.expire_stale_orders(session)


@job("expire_orphan_offers", IntervalTrigger(hours=1), "pending offers of closed orders → expired")
async def expire_orphan_offers() -> dict[str, Any]:
    async with transaction() as session:
        return {"offers_closed": await expiry.expire_orphan_offers(session)}


@job("courier_presence_expiry", IntervalTrigger(minutes=5), "online couriers silent for 15 min → offline")
async def courier_presence_expiry() -> dict[str, Any]:
    async with transaction() as session:
        return {"set_offline": await couriers.expire_presence(session)}


async def build_commission_statements() -> dict[str, Any]:
    async with transaction() as session:
        return await commission.build_statements(session)


# Monday 04:00 in Tunis (the scheduler itself runs in UTC).
STATEMENTS_TRIGGER = CronTrigger(day_of_week="mon", hour=4, minute=0, timezone="Africa/Tunis")
