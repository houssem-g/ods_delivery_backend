"""listOrderDrafts — the caller's live drafts (not expired), newest first.

Returns { drafts: [{ id, title, payload, created_date, updated_date, expires_at }] }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import order_drafts


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    drafts = await order_drafts.list_live(session, user.id)
    return 200, {"drafts": [order_drafts.as_dict(d) for d in drafts]}
