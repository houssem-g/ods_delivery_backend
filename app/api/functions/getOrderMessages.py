"""getOrderMessages — the chat of one order, for its parties.

Body { order_id, limit?, mark_read? }. Returns { success, messages, marked? } (`marked` only
with mark_read: the caller's unread incoming messages are marked read in the same call).
Errors: Missing order_id (400), Order not found (404), Forbidden (403)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import messages


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await messages.get_order_messages(session, user, payload)
