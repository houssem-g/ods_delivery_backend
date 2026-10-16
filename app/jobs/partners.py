"""Partner jobs: the outgoing webhook queue (app/services/partner_webhooks.py)."""

from typing import Any

from apscheduler.triggers.interval import IntervalTrigger

from app.db import transaction
from app.jobs.registry import job
from app.services import partner_webhooks


@job(
    "deliver_partner_webhooks",
    IntervalTrigger(seconds=30),
    "signed webhooks to the partner restaurants: due rows posted, retried 1 → 16 min",
)
async def deliver_partner_webhooks() -> dict[str, Any]:
    async with transaction() as session:
        return await partner_webhooks.deliver_due(session)
