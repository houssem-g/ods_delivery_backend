"""markNotificationsRead — mark several of the caller's notifications read in ONE call.

The Notifications page used to send one `Notification.update` per row (28 requests at once on a
mobile network, QA 06/10 B59). Body { ids: [notification id, …] } (1..500; the page sends the
rows of the mode it shows, so a dual account's other mode keeps its badge). Only the caller's
own unread rows change; unknown ids, other people's rows and rows already read are ignored.
Returns { success, marked } (how many rows went from unread to read).
Errors: invalid_ids (400: missing, empty, not a list, more than 500)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import notifications


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await notifications.mark_read(session, user, payload.get("ids"))
