"""getVitrineRequestAccess — account optional: { ods_restaurant_id? } (+ X-Partner-Key) → { success, open }:
may this caller ask to have a menu on ODS Delivery (switch « Vitrines publiques autorisées » OFF → admins,
QA accounts and proven ODS restaurants only). Rules: app/services/vitrine_requests.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import vitrine_requests

AUTH = "optional"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine_requests.access(session, user, payload, request.headers.get("x-partner-key"))
