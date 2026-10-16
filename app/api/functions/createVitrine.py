"""createVitrine — admin: { shop_id? | shop:{…}, source, partner_id?, hours?, slug? } → { success, vitrine }
(draft, qa)
Vitrine, contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import vitrine


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine.create_vitrine(session, user, payload)
