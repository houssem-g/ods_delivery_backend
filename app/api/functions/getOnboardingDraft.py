"""getOnboardingDraft — the caller's unfinished sign-up of this kind (not expired), or null.

Body: { kind: 'courier' | 'customer' }. Returns { draft: { kind, payload, created_date,
updated_date, expires_at } | null }. Errors: invalid_kind (400).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import onboarding_drafts


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    try:
        draft = await onboarding_drafts.get_live(session, user.id, payload.get("kind"))
    except onboarding_drafts.DraftRefused as exc:
        return exc.status, exc.body()
    return 200, {"draft": onboarding_drafts.as_dict(draft) if draft else None}
