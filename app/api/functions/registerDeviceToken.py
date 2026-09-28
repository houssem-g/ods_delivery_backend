"""registerDeviceToken — upsert a push token for the caller.

Body { token, platform: web|android|ios (detected from the User-Agent when absent),
provider?: fcm|webpush, app_version?, device_model?, locale? }. Returns { success, device_token_id }."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import device_tokens


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await device_tokens.register(session, user, payload, request.headers.get("user-agent"))
