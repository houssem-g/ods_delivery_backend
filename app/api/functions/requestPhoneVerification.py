"""requestPhoneVerification — send a 6-digit code by WhatsApp to confirm a foreign phone number.

Body: { phone }. Tunisian numbers need no code: 200 { verified: true, required: false }.
Otherwise 200 { sent: true, channel: 'whatsapp', expires_in } (seconds).
Errors: phone_required / invalid_phone / no_whatsapp (400), too_many_codes (429,
retry_after_seconds), whatsapp_failed (502), whatsapp_unavailable (503, WhatsApp not configured).
A refusal after a code row was written is committed: the rate limits count it.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services.phone_verification import VerificationRefused, request_code


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    try:
        return 200, await request_code(session, user.id, payload.get("phone"))
    except VerificationRefused as exc:
        await session.commit()
        return exc.status, exc.body()
