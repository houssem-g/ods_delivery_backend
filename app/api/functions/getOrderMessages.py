"""getOrderMessages — the chat of one order, for its parties.

Body { order_id, limit?, mark_read? }. Returns { success, messages, marked? } (`marked` only
with mark_read: the caller's unread incoming messages are marked read in the same call). Each
message also has attachment_url (signed for 10 min) / attachment_type / attachment_duration
(null without an attachment), read_at (ISO, only on the caller's own messages, else null) and
translation ({target, text, source_lang} when translateOrderMessage already stored one in the
caller's language, else null; never an API call here).
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
