"""reportPartnerReturn — the order's courier or an admin: { order_id, outcome: 'refused' | 'shop_closed' }
→ { success, outcome, returns_refused, debt_amount } (D4/D5).
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
    return 200, await partners.report_return(session, user, payload)
