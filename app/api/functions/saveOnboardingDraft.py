"""saveOnboardingDraft — keep the unfinished sign-up (courier onboarding, customer setup) 30 days.

Body: { kind: 'courier' | 'customer', payload } (payload: the form state, a JSON object of 16 KB
at most; keys named like a password / token / secret / otp are dropped). One draft per kind:
saving again replaces it. Returns { draft: { kind, payload, created_date, updated_date,
expires_at } }. Errors: invalid_kind / invalid_payload (400), payload_too_large (413).
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
        draft = await onboarding_drafts.save(session, user.id, payload.get("kind"), payload.get("payload"))
    except onboarding_drafts.DraftRefused as exc:
        return exc.status, exc.body()
    return 200, {"draft": onboarding_drafts.as_dict(draft)}
