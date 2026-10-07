"""grantCourierCredit — admin: a prime paid in credit or a signed correction. Body: { courier_id, kind (prime
| adjustment), amount, reason }.
See app/services/credit.py (decision D-10: the courier's commission is prepaid)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import credit


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await credit.grant(session, user, payload)
