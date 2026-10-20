"""listAdminUsers — admin only, read only (owner 20/10): see app/services/admin_lists.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import admin_lists


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return await admin_lists.list_users(session, user, payload)
