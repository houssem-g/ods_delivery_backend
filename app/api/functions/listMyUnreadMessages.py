"""listMyUnreadMessages — the caller's unread incoming chat messages.

Body { mode: 'fast' } (messages addressed to him) or {} (full scan of his orders, incl. the
customer's answers on open orders he asked about). Returns { success, messages, mode }."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import messages


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await messages.list_my_unread_messages(session, user, payload)
