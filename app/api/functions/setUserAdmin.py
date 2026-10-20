"""setUserAdmin — super admin only (owner 20/10): give or remove the admin role.
See app/services/super_admin.py. Payload: {user_id, admin: true|false}."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import super_admin


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return await super_admin.set_user_admin(session, user, payload, request)
