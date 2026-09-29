"""saveOrderDraft — keep the unfinished NewOrder form as a draft for 24 h.

Body: { id?, payload, title? } (payload: the form state, a JSON object of 20 KB at most).
Updates the caller's live draft `id`, otherwise creates one (max 10 live drafts: the oldest is
dropped). Returns { draft: { id, title, payload, created_date, updated_date, expires_at } }.
Errors: invalid_payload (400), payload_too_large (413).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import order_drafts


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    try:
        draft = await order_drafts.save(
            session, user.id, payload.get("id"), payload.get("payload"), payload.get("title")
        )
    except order_drafts.DraftRefused as exc:
        return exc.status, exc.body()
    return 200, {"draft": order_drafts.as_dict(draft)}
