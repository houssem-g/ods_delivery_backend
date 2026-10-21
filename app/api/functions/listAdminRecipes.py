"""listAdminRecipes — admin: {} → { success, recipes (drafts included, status, cost, orders), public_allowed,
prices: { available, fetched_at, priced } }."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import recipes


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await recipes.admin_list(session, user, payload)
