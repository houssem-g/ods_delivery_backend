"""uploadCourierDocument — the courier sends (or replaces) one of his documents
(app/services/courier_documents.py).

Body: { kind (cin | permis | carte_grise | assurance | photo), file_url (his own private upload:
UploadPrivateFile's file_uri, image or PDF), expires_on? (YYYY-MM-DD, not in the past) }.
A new upload replaces the file and resets the review (pending). Returns { success, document }.
Errors: invalid_kind (+ kinds) / invalid_file / invalid_expires_on / document_expired (400),
courier_profile_missing (403).
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
    return 200, await courier_documents.upload(session, user, payload)
