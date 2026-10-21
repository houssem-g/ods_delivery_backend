"""getCourierDocumentSummaries — admin only (owner 21/10): each courier's expected documents and their
state (app/services/courier_documents.py, summary). No file link: the picture is opened through
listCourierDocuments (5-minute signed link) or getCourierIdPhotos.

Body: { courier_ids: string[] } (at most 100). Returns { success, documents: { [courier_id]: { items:
[{ kind, required, status (missing | pending | verified | rejected), expires_on, expired, document_id,
source (upload | onboarding | null), note }], missing, to_review, rejected, expired, ready } } }.
Errors: courier_ids_required (400), Forbidden (403).
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
    return 200, await courier_documents.summaries_for_ids(session, user, payload)
