"""planRecipeWeek — client: { people (1–12), nights (3–7), budget? (DT, null = libre), cuisines? [..],
no_red_meat?, no_fish?, quick?, have? [ingredient ids of « Avec ce que j'ai »], fixed? [{day, recipe_id}] (the
padlocks, or every other evening when one is changed), avoid? [recipe ids] (« Changer »), seed?, today? }
→ « Ma semaine » (owner 21/10): one dinner per evening chosen by rules, no AI (app/services/recipes_week.py):
{ success, ok, days [{day, card, protein, shared}], total {low, high}, savings, shared, baskets (≤ 80 DT
each), at_home } or { success, ok: false, reason: budget (+ min_budget) | few | no_other }. ADMINS ONLY for
now (403)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import recipes_week


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await recipes_week.plan(session, user, payload)
