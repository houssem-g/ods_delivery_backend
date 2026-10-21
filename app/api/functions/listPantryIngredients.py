"""listPantryIngredients — « Avec ce que j'ai »: {} → { success, ingredients: [{ id, key, name_fr,
name_ar, darija[], syn[], pantry, aisle, uses }], quick: [ids], placard: [ids], families: [[ids]] } — the
dictionary the phone searches while the customer types (« tmatem », « bsal », « felfel »).
ADMINS ONLY (403 otherwise)."""

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
    return 200, await recipes_pantry.list_ingredients(session, user, payload)
