"""unregisterDeviceToken — deactivate one of the caller's push tokens (logout...).

Body { token? , endpoint_hash? } (one required). Returns { success, deactivated }."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import device_tokens


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await device_tokens.unregister(session, user, payload)
