"""setRecipeStatus — admin: { id | ids: [...] (≤ 1000), status: published | draft } → { success, id, status,
published_at, changed } — publish / unpublish one recipe or many (audit log per recipe). 404 not_found,
409 recipe_incomplete (one recipe without ingredient; in bulk such a recipe is skipped)."""

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
    return 200, await recipes.set_status(session, user, payload)
