"""sendNotificationIfEnabled — in-app notification + push (preferences govern the push only).

Body { userId, type, title_ar, title_fr, body_ar, body_fr, order_id?, metadata? }.
Returns { success, notification_id, push_skipped? }. Authorization matrix:
app/services/notification_requests.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import notification_requests


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await notification_requests.send_notification_if_enabled(session, user, payload)
