"""markNotificationsRead — mark several of the caller's notifications read in ONE call.

The Notifications page used to send one `Notification.update` per row (28 requests at once on a
mobile network, QA 06/10 B59). Body { ids: [notification id, …] } (1..500; the page sends the
rows of the mode it shows, so a dual account's other mode keeps its badge). Only the caller's
own unread rows change; unknown ids, other people's rows and rows already read are ignored.
Returns { success, marked } (how many rows went from unread to read).
Errors: invalid_ids (400: missing, empty, not a list, more than 500).
« Tout lire » (QA 19/10 M10): body { all: true } marks EVERY unread notification of the caller (both
modes of a dual account), not only the loaded rows; returns { success, marked, unread: 0 }. His screens
hear one Notification `signal` {kind: "all_read"} (id = his user id) to re-count, plus one `update` per
row when there are at most 100."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import notifications


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    if payload.get("all") is True:
        return await notifications.mark_all_read(session, user)
    return await notifications.mark_read(session, user, payload.get("ids"))
