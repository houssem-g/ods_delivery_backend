"""saveMyOrderFrequency — customer: « vous vous faites livrer combien de fois par mois ? ». Body: {
monthly_orders (0–60) | null }.
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
    return 200, await forecast.save_order_frequency(session, user, payload)
