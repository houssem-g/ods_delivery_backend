"""getRecipe — client: { id | slug, servings? (1–20), at_home? [ingredient ids], with_optional? [ids] } →
{ success, recipe }. An optional line (« facultatif ») is bought only when ticked in `with_optional`.
The exact quantities to cook for `servings`, each ingredient's line to buy (rounded: kg, L, pièces, bottes)
unless it is « j'ai déjà » (pantry pre-ticked when `at_home` is not sent), the honest cost, the steps.
Unknown / unpublished / not visible → 404 not_found (an admin also reads a draft)."""

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
    return 200, await recipes.get_recipe(session, user, payload)
