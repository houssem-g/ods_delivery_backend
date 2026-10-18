"""deleteOnboardingDraft — drop the caller's sign-up draft of this kind (finished or abandoned).

Body: { kind: 'courier' | 'customer' }. Returns { success: true, deleted: bool } (deleting a
draft that is not there is not an error). Errors: invalid_kind (400).
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
        deleted = await onboarding_drafts.remove(session, user.id, payload.get("kind"))
    except onboarding_drafts.DraftRefused as exc:
        return exc.status, exc.body()
    return 200, {"success": True, "deleted": deleted}
