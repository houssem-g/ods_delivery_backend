"""Vitrine jobs: the visits kept for the one-visit-per-device-and-day rule are purged after 40 days
(the daily counters stay in vitrine_stats_daily)."""

from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.db import transaction
from app.jobs.registry import job
from app.services import vitrine_stats


@job(
    "purge_vitrine_visits",
    CronTrigger(hour=3, minute=40, timezone="Africa/Tunis"),
    "Vitrine: visits older than 40 days purged (the daily counters stay)",
)
async def purge_vitrine_visits() -> dict[str, Any]:
    async with transaction() as session:
        return {"deleted": await vitrine_stats.purge_visits(session)}
