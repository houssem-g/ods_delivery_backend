"""getRecipeOrder — client (admins only while Recettes is): { order_id } → what « Ma liste » needs to make a
recipe order again: { success, recipes: [card + servings + with_optional], at_home, extras: [{name, count}],
dropped: [titles no longer available] } (QA 22/10: « Commander à nouveau » goes back through the recipes).
Refusals: 403 forbidden (not an admin), 404 not_found (no such recipe order of this customer)."""

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
    return 200, await recipes.order_again(session, user, payload)
