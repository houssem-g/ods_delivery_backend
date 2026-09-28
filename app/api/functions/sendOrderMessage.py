"""sendOrderMessage — post a chat message on an order and notify the other party.

Body { order_id, content }. Returns { success, message, notified }. Errors (JSON `error`):
Missing order_id / invalid_content (400, + `max`), order_not_found (404), not_a_party (403),
order_closed (409), too_many_messages (429). Logic: app/services/messages.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import messages


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await messages.send_order_message(session, user, payload)
