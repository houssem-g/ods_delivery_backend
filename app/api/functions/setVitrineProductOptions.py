"""setVitrineProductOptions — admin: { shop_id, product_id, hidden?: [key], added?: [{name_fr, name_ar?,
levels}], order?: [key], reset?: true } → { success, product } (also on a catalog linked to ODS).
Vitrine options « ce que j'enlève », contract: ods-audit/livraison-1610/vitrine/options/CONTRAT-OPTIONS.md."""

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
    return 200, await vitrine.set_product_options(session, user, payload)
