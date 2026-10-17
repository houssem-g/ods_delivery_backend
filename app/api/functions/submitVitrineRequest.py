"""submitVitrineRequest — a restaurant asks to have its menu on ODS Delivery (account optional).

Body: { shop_name, phone, address, lat?, lng?, kind?, uses_ods?, menu_photo_key? (signed in: a
private upload of his, purpose « menu »), message?, lang?, website (hidden field: must stay empty) }
— or, from the ODS backend: { source: "ods", ods_restaurant_id, test, … } (+ header X-Partner-Key of
that restaurant when it is linked: then trusted). Answers { success, request, follow_token } (the
token, shown once, lets the device / ODS follow the state; a request not stored answers the same).
Errors 400 invalid_name / invalid_phone / invalid_address / invalid_location / invalid_kind /
invalid_photo / validation_error, 429 too_many_requests. Only an admin creates the Vitrine.
Rules: app/services/vitrine_requests.py."""

from typing import Any

from fastapi import Request
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import vitrine_requests

AUTH = "optional"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine_requests.submit(
        session, user, payload, get_remote_address(request), request.headers.get("x-partner-key")
    )
