"""setDispatchSettings — admin: { notify_offline_couriers: true | false } — switch the launch mode on or off.
See app/services/launch_mode.py (owner, 13/10/2026)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import launch_mode


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await launch_mode.set_settings(session, user, payload)
