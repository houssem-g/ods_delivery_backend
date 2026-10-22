"""importRecipeVideos — admin (22/10/2026): records the videos of the Recettes from data/recipes/videos.json.
{ gestures: [{key, name_fr, name_ar, clip? | youtube_id + start + end, credit, triggers_fr, triggers_ar}],
  passages: {"<slug>": [{step, youtube_id, start, end, title, channel}]} }  (or { videos: that, prune? })
→ { success, gestures: {created, updated, unchanged, removed}, passages: {…}, unknown_recipes, refused }.
Idempotent; a recipe's passages absent from the file are removed; `prune` also removes absent gestures.
A clip only under public/recipes/gestures/*.webm, a YouTube id of 11 characters, a passage ≤ 10 min.
(app/services/recipe_videos.py)"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import recipe_videos


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await recipe_videos.import_admin(session, user, payload)
