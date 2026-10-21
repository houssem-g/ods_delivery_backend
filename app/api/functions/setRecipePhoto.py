"""setRecipePhoto — admin (22/10/2026): records the dish photo of one recipe or of many at once.
One recipe: { slug | id, photo: url | key | null, thumb?: url | key, alt_fr?, alt_ar?, source?: ia | photo }.
A whole file: { photos: { "<slug>": url | key | {photo, thumb?, alt_fr?, alt_ar?, source?} | null } }
(≤ 1000 entries).
→ { success, updated, cleared, unchanged, unknown: [slug], refused: [reason] }.
Accepted: a key under public/recipes/ ending in .webp, or a URL of our own public files origin (the bucket's
public URL, like the Vitrine thumbnails); `photo: null` removes it. Audit row per recipe changed.
(app/services/recipe_photos.py)"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import recipe_photos


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await recipe_photos.set_photos(session, user, payload)
