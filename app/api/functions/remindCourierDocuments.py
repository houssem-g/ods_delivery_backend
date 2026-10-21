"""remindCourierDocuments — admin only (owner 21/10): « Relancer » a courier for documents
(app/services/courier_reminders.py).

Body: { courier_id, kinds: string[], note? (≤ 200) }. Returns { success, reminder: { at, next_at, kinds,
email_sent }, texts }. Errors: invalid_kinds / invalid_note (400), Forbidden (403), courier_not_found (404),
reminded_recently (429, `last`: at most one reminder per courier every 12 h).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import courier_reminders


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await courier_reminders.remind(session, user, payload, request)
