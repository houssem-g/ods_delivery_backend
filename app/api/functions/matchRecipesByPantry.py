"""matchRecipesByPantry — « Avec ce que j'ai » (owner 21/10): { have: [ingredient id | word (« tmatem »)],
pantry_off?: [ids], use_pantry? (true), cuisine?, country?, category?, tag?, max_time?, q?, per_group? }
→ { success, have, unknown, groups: [{ missing: 0..3, total, recipes: [card + have_n, missing[], missing_cost,
at_home[]] }], total }. Exact rules, no AI: app/services/recipes_pantry.py. ADMINS ONLY (403 otherwise)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import recipes_pantry


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await recipes_pantry.match_recipes(session, user, payload)
