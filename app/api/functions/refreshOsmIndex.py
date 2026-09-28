"""refreshOsmIndex — Overpass import of one category (or all) into `places`.

Auth: a signed-in admin, or the header `x-cron-token: <CRON_SECRET>` (an unset secret
never matches). Anyone else: 401 { error: 'Unauthorized' } (the Base44
`_internal_key` does not exist here: internal callers use the service directly).
Body / query `category`: one of the 7 keys, 'all', or nothing (the weekday's category,
UTC); unknown → 400. Answers { success, duration_ms, results[], log[], backfilled }.
The daily job `osm_refresh` (app/jobs/placeholders.py) runs the same service.
"""

import secrets
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.security.deps import CurrentUser
from app.services.osm_refresh import CATEGORY_RULES, UnknownCategory, run_refresh, targets_for

AUTH = "optional"


def _cron_token_ok(request: Request) -> bool:
    token = request.headers.get("x-cron-token")
    return bool(settings.CRON_SECRET and token and secrets.compare_digest(token, settings.CRON_SECRET))


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not ((user is not None and user.is_admin) or _cron_token_ok(request)):
        return 401, {"error": "Unauthorized"}
    requested = str(payload.get("category") or request.query_params.get("category") or "")
    try:
        rules = targets_for(requested)
    except UnknownCategory:
        valid = ", ".join(rule.key for rule in CATEGORY_RULES)
        return 400, {"error": f"Unknown category '{requested.strip()}'. Valid: {valid}"}
    return 200, await run_refresh(session, rules)
