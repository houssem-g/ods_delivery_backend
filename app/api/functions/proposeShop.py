"""proposeShop — a signed-in user suggests a shop missing from the map ("Nouveau magasin").

Body: { name, address, latitude, longitude, phone?, opening_hours?, category?,
description?, menu_items? }. Answers { success, shop: {id, name, review_status},
duplicate? }. Errors (JSON `error`, 400): invalid_name, invalid_address,
invalid_location, invalid_phone, invalid_category; 429 too_many_proposals (+ max).
Rules: app/services/shops.py.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.security.deps import CurrentUser
from app.services.shops import propose_shop


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    try:
        return 200, await propose_shop(session, user, payload)
    except ApiError as exc:
        body = {"error": exc.error, **exc.extra}
        if exc.message != exc.error:
            body["message"] = exc.message
        return exc.status, body
