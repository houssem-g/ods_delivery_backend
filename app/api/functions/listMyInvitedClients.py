"""listMyInvitedClients — the courier's invited clients (first name), their delivered orders and his monthly
estimate.
See app/services/forecast.py (courier earnings forecast, self-correcting)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import forecast


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await forecast.list_invited(session, user)
