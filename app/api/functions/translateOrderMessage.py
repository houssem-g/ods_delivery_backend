"""translateOrderMessage — a chat message translated for the caller (app/services/translation.py).

Body { message_id, target? ('fr' | 'ar'; default: the caller's language) }. Same access as
getOrderMessages: the order's customer, its courier, a courier asking before assignment (only the
messages he sees), an admin. Returns { success, available, message_id, target, source_lang
('fr' | 'ar' | 'derja' | 'arabizi' | 'other' | null), translation (null when the message is
already in the target language, empty or attachment-only), cached, reason? }. available: false
comes with reason: disabled (no TRANSLATE_API_KEY) | budget (month's budget spent) | unavailable
(the endpoint failed or answered nonsense) — never an error status.
Errors: invalid_message_id / invalid_target (400), not_a_party (403), message_not_found (404),
too_many_translate_requests (429, RATE_LIMIT_TRANSLATE per user).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import translation


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    if not allow("translateOrderMessage", f"user:{user.id}", settings.RATE_LIMIT_TRANSLATE):
        # own code, not the generic limiter's text: the front pauses all its polls on that one
        return 429, {"error": "too_many_translate_requests", "limit": settings.RATE_LIMIT_TRANSLATE}
    return await translation.translate_order_message(session, user, payload)
