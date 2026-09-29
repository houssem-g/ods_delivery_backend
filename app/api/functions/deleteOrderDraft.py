"""deleteOrderDraft — delete one of the caller's drafts.

Body: { id }. Returns { success: true }; 404 { error: 'draft_not_found' } for an unknown id or
another user's draft.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import order_drafts


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not await order_drafts.remove(session, user.id, payload.get("id")):
        return 404, {"error": "draft_not_found"}
    return 200, {"success": True}
