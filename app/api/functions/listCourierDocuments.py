"""listCourierDocuments — the admins' review list (app/services/courier_documents.py).

Body: { courier_id?, status? (pending | verified | rejected) }. Returns { success, documents:
[{ ...listMyCourierDocuments' fields, courier_name, file_url (signed, 5 minutes), url_expires_in }] }
(200 at most, most recently changed first). Errors: invalid_status (400), Forbidden (403).
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
    return 200, await courier_documents.list_all(session, user, payload)
