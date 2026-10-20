"""getAdminCourierSheet — admin only, read only (owner 20/10): see app/services/admin_sheets.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import admin_sheets


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return await admin_sheets.courier_sheet(session, user, payload)
