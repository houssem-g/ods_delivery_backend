"""setVitrineCatalogSource — admin: { shop_id, source: ods, partner_id } | { shop_id, source: manual, mode:
keep|empty|restore }, dry_run? → { success, vitrine | preview }
Vitrine, contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import vitrine_catalog


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine_catalog.set_source(session, user, payload)
