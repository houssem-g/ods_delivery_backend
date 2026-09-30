"""getTranslationStatus — admins: is chat translation on, and this month's spend.

Body {}. Returns { success, enabled (TRANSLATE_API_KEY set), model, month ('YYYY-MM', UTC),
month_cost_usd, budget_usd, calls }. Errors: Forbidden (403, non-admin).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import translation


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await translation.translation_status(session, user)
