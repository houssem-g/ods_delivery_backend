"""saveMyCourierActivity — what the courier already does outside the app. Body: { weekly_deliveries? (0–300),
active_days? (0–7), regular_clients? (0–500) }.
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
    return 200, await forecast.save_activity(session, user, payload)
