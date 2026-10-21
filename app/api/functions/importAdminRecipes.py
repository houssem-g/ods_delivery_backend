"""importAdminRecipes — admin: { recipes: a recipe file (recettes*.json) or a list of them, new_ingredients?:
[the ingredients_a_ajouter_*.json contents], ingredients?: [dictionary] } → { success, ingredients:
{created, updated, refused} | null, created, updated, unchanged, back_to_draft: [slug], refused: [reason],
warnings: [line kept without a price…] }.
Idempotent (app/services/recipes_import.py): upsert by slug, new = draft, a published recipe that changed
goes back to draft. Without `ingredients`, the dictionary sent by ODS with its prices is used (when read)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.errors import ApiError
from app.security.deps import CurrentUser
from app.services import recipe_prices, recipes_import


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if user is None or not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    entries = payload.get("ingredients")
    if not isinstance(entries, list):
        prices = await recipe_prices.prices(session)
        entries = (prices or {}).get("ingredients") if isinstance(prices, dict) else None
    ingredients = (
        await recipes_import.import_ingredients(session, entries) if isinstance(entries, list) else None
    )
    report = await recipes_import.import_recipes(
        session, payload.get("recipes"), payload.get("new_ingredients")
    )
    return 200, {"success": True, "ingredients": ingredients, **report}
