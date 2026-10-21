"""listRecipes — client: {} → { success, visible, test, recipes: [card], prices_available }
Recettes (21/10/2026, app/services/recipes.py): visible to admins and the test accounts while the switch
« Recettes publiques » is off — anyone else reads { visible: false, recipes: [] }. A card carries the honest
cost for its base number of people (pantry ticked « j'ai déjà »): { available, show_total, low, high, priced,
unpriced[{id, fr, ar}], oldest_as_of, stale, to_check, sources[] }, low / high null unless show_total."""

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
    return 200, await recipes.list_recipes(session, user, payload)
