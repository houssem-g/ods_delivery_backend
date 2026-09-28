"""Daily and weekly jobs of ARCHITECTURE §8: the OSM places refresh and the courier commission
statements. The 5-minute / hourly jobs live with their domain: app/jobs/orders.py,
app/jobs/messaging.py, app/jobs/incidents.py."""

from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import transaction
from app.jobs.orders import STATEMENTS_TRIGGER, build_commission_statements
from app.jobs.registry import job
from app.services.osm_refresh import run_refresh, targets_for


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


@job("courier_statements", STATEMENTS_TRIGGER, "weekly commission statements (previous weeks' due entries)")
async def courier_statements() -> dict[str, Any]:
    return await build_commission_statements()
