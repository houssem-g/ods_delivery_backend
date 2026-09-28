"""The scheduled jobs of ARCHITECTURE §8, registered with their triggers. Their bodies are
ported by the business agents; until then they only log."""

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import transaction
from app.jobs.registry import job
from app.services.osm_refresh import run_refresh, targets_for

log = logging.getLogger("odsd.jobs")


def _placeholder(name: str) -> dict[str, Any]:
    log.info("job %s: not implemented yet", name)
    return {"implemented": False}


@job(
    "sweep_5min",
    IntervalTrigger(minutes=5),
    "no-response sweep, WhatsApp/SMS pending checks, stale order expiry, courier presence expiry",
)
async def sweep_5min() -> dict[str, Any]:
    return _placeholder("sweep_5min")


@job(
    "hourly_cleanup", IntervalTrigger(hours=1), "expired hot deals, offers of closed orders, test data purge"
)
async def hourly_cleanup() -> dict[str, Any]:
    return _placeholder("hourly_cleanup")


@job(
    "osm_refresh",
    CronTrigger(hour=3, minute=0),
    "OSM places refresh, the weekday's category (Overpass); on with OSM_REFRESH_ENABLED",
    enabled=settings.OSM_REFRESH_ENABLED,
)
async def osm_refresh() -> dict[str, Any]:
    async with transaction() as session:
        result = await run_refresh(session, targets_for(""))
    return {"results": result["results"], "backfilled": result["backfilled"]}


@job("courier_statements", CronTrigger(day_of_week="mon", hour=4, minute=0), "weekly commission statements")
async def courier_statements() -> dict[str, Any]:
    return _placeholder("courier_statements")
