"""confirmPhoneVerification — check the WhatsApp code; on success the number becomes the
profile's phone, verified.

Body: { phone, code }. Returns 200 { verified: true, phone }.
Errors (400): phone_required, invalid_phone, invalid_code (attempts_left), code_expired,
too_many_attempts (5 wrong codes: ask a new one). Every attempt is committed.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services.phone_verification import VerificationRefused, confirm_code


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    try:
        return 200, await confirm_code(session, user.id, payload.get("phone"), payload.get("code"))
    except VerificationRefused as exc:
        await session.commit()
        return exc.status, exc.body()
