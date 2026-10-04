"""listBlockedUsers — see app/services/safety.py (blocking and reporting users, App Review 1.2)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import safety


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await safety.list_blocked_users(session, user, payload)
