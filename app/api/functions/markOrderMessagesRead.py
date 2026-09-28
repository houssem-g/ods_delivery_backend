"""markOrderMessagesRead — mark the caller's unread incoming messages of an order read.

Body { order_id }. Returns { success, marked }. Errors as getOrderMessages."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import messages


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await messages.mark_order_messages_read(session, user, payload)
