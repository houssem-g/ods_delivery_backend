"""listMyVitrineRequests — signed in: { } → { success, requests } (his own requests, newest first).
Rules: app/services/vitrine_requests.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import vitrine_requests


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine_requests.list_mine(session, user, payload)
