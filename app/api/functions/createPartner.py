"""createPartner — admin: { name, ods_restaurant_id?, webhook_url?, max_open_orders? } → { success,
partner, api_key, webhook_secret } (key and secret shown ONCE).
See app/services/partners.py and ods-audit/livraison-1610/lot2/2A-CONTRAT.md (Lot 2, 2A)."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import partners


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await partners.create_partner(session, user, payload)
