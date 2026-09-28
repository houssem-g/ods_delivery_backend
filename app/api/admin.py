"""/api/admin/jobs — list the scheduled jobs and run one by hand.

Allowed for an admin session, or with the header `x-cron-token: <CRON_SECRET>` (external cron)."""

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header

from app.config import settings
from app.errors import ApiError
from app.jobs.registry import JOBS
from app.jobs.scheduler import run_job
from app.security.deps import CurrentUser, optional_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def cron_or_admin(
    x_cron_token: str | None = Header(default=None),
    user: CurrentUser | None = Depends(optional_user),
) -> None:
    if user is not None and user.is_admin:
        return
    if settings.CRON_SECRET and x_cron_token and secrets.compare_digest(x_cron_token, settings.CRON_SECRET):
        return
    raise ApiError(403, "forbidden", "Admin session or cron token required")


@router.get("/jobs", dependencies=[Depends(cron_or_admin)])
async def list_jobs() -> list[dict[str, Any]]:
    return [
        {"name": j.name, "description": j.description, "trigger": str(j.trigger), "enabled": j.enabled}
        for j in JOBS.values()
    ]


@router.post("/jobs/{name}/run", dependencies=[Depends(cron_or_admin)])
async def run(name: str) -> dict[str, Any]:
    job = JOBS.get(name)
    if job is None:
        raise ApiError(404, "not_found", f"Job {name} not found")
    return await run_job(job)
