"""resolveReferralCode — courier invite code → public courier card. Works signed out
(Welcome banner) and signed in (RoleSelection, before the customer profile is created).

Body { code }. Always 200: { valid: true, code, courier_id, first_name, rating,
total_deliveries, vehicle_type } or { valid: false, reason: invalid_code | unknown_code |
self (+ is_self) }. Never a phone, e-mail, ID number, position or photo.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services.referral import resolve_code

AUTH = "optional"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await resolve_code(session, payload.get("code"), user)
