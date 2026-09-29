"""reviewCourierDocument — an admin verifies or rejects a courier document
(app/services/courier_documents.py). The courier is told (document_verified / document_rejected).

Body: { id, status (verified | rejected), note? (≤ 500) }. Returns { success, document }.
Errors: invalid_status / invalid_note (400), Forbidden (403), document_not_found (404).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import courier_documents


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await courier_documents.review(session, user, payload)
