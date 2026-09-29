"""listMyCourierDocuments — the courier's documents (app/services/courier_documents.py).

Body: {}. Returns { success, documents: [{ id, courier_id, kind (cin | permis | carte_grise |
assurance | photo), status (pending | verified | rejected), expires_on (YYYY-MM-DD | null), expired,
note, reviewed_at, created_date, updated_date, synthetic }] } — without an uploaded `cin`, a
synthetic one ({id: null, synthetic: true, has_file}) mirrors his verification status. No file links.
Errors: courier_profile_missing (403).
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
    return 200, await courier_documents.list_mine(session, user)
