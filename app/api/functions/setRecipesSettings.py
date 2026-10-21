"""setRecipesSettings — admin: { public_allowed: bool } → { success, public_allowed } (audit log).
Turning it on shows the published recipes to every account: owner's explicit OK only."""

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
    return 200, await recipes.set_settings(session, user, payload)
