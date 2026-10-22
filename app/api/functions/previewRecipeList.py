"""previewRecipeList — client: { items: [{ recipe_id, servings, with_optional? }], at_home?: [ingredient ids],
extras? } → { success, recipes, lines (by aisle: marché, boucherie, poissonnerie, crèmerie, boulangerie,
épicerie), at_home, optional (not bought, to tick), weight_kg (typed articles included), cost, cap (80 DT),
over_cap, budget_max (the order's ceiling) }. Refusals: 404 recipes_unavailable (not
visible), 400 recipes_empty, too_many_recipes (+ max), 409 recipes_unavailable (+ ids: unpublished)."""

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
    return 200, await recipes.preview_list(session, user, payload)
