"""/api/partner/v1 — the restaurants ODS create and follow deliveries (Lot 2, 2A/2B).

Authentication: `X-Partner-Key` (app/security/partner.py); 120 requests/min per key. Errors
`{error, message, …}` (app/errors.py). Dates ISO 8601 UTC « Z ». Full contract:
ods-audit/livraison-1610/lot2/2A-CONTRAT.md.

    GET  /availability?lat&lng                 couriers around a point, nobody notified
    POST /orders                               Idempotency-Key / partner_ref → 201 (200 on replay)
    GET  /orders/{id}                          the restaurant's view (courier phone included)
    GET  /orders/{id}/offers
    POST /orders/{id}/offers/{offer_id}/accept  (alias POST /offers/{offer_id}/accept)
    POST /orders/{id}/cancel {reason}          free until accepted; then 409 cancel_not_allowed
    GET  /track/{token}                        public, no key (300/h per IP): no phone, no address
    POST /ping, POST /webhook-test
    PUT  /catalog                              Vitrine: the restaurant's full menu snapshot (idempotent)
    POST /vitrine-orders/{ref}/missing-items   Vitrine: a dish is missing → the customer is asked
(Vitrine contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md §6–§7.)
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import ApiError
from app.models import Order, OrderOffer
from app.rate_limit import allow
from app.security.partner import PartnerContext, current_partner
from app.services import partner_orders, partners, vitrine_catalog, vitrine_orders
from app.services.partner_orders import PartnerOrderIn

router = APIRouter(prefix="/api/partner/v1", tags=["partner"])


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    expected_fee: float | None = Field(default=None, ge=0)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1, max_length=200)


@router.get("/availability")
async def availability(
    lat: float = Query(...),
    lng: float = Query(...),
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    body = await partner_orders.availability(session, lat, lng)
    await session.commit()  # last_used_at
    return body


@router.post("/orders")
async def create_order(
    body: PartnerOrderIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    status, answer = await partner_orders.create_order(session, ctx, body, idempotency_key)
    await session.commit()
    return JSONResponse(answer, status_code=status)


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    order = await partner_orders.partner_order(session, ctx, order_id)
    view = await partner_orders.order_view(session, order)
    await session.commit()
    return view


@router.get("/orders/{order_id}/offers")
async def list_offers(
    order_id: str,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    order = await partner_orders.partner_order(session, ctx, order_id)
    offers = await partner_orders.offers_view(session, order)
    await session.commit()
    return {"order_id": str(order.id), "status": order.status, "offers": offers}


async def _accept(
    session: AsyncSession, ctx: PartnerContext, order: Order, offer_id: str, body: AcceptIn | None
) -> dict[str, Any]:
    answer = await partner_orders.accept(
        session, ctx, order, offer_id, body.expected_fee if body is not None else None
    )
    await session.commit()
    return answer


@router.post("/orders/{order_id}/offers/{offer_id}/accept")
async def accept_offer(
    order_id: str,
    offer_id: str,
    body: AcceptIn | None = None,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    order = await partner_orders.partner_order(session, ctx, order_id)
    return await _accept(session, ctx, order, offer_id, body)


@router.post("/offers/{offer_id}/accept")
async def accept_offer_short(
    offer_id: str,
    body: AcceptIn | None = None,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        import uuid

        oid = uuid.UUID(offer_id)
    except ValueError:
        raise ApiError(404, "offer_not_found", "Offer not found") from None
    order_id = (
        await session.execute(select(OrderOffer.order_id).where(OrderOffer.id == oid))
    ).scalar_one_or_none()
    if order_id is None:
        raise ApiError(404, "offer_not_found", "Offer not found")
    order = await partner_orders.partner_order(session, ctx, str(order_id))
    return await _accept(session, ctx, order, offer_id, body)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    body: CancelIn,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    order = await partner_orders.partner_order(session, ctx, order_id)
    answer = await partner_orders.cancel(session, ctx, order, body.reason)
    await session.commit()
    return answer


@router.get("/track/{token}")
async def track(token: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not allow("partner_track", get_remote_address(request), settings.RATE_LIMIT_PARTNER_TRACK):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    token = token.strip()
    order = None
    if 16 <= len(token) <= 64:
        order = (
            await session.execute(select(Order).where(Order.tracking_token == token))
        ).scalar_one_or_none()
    if order is None:
        raise ApiError(404, "not_found", "Unknown tracking link")
    return await partner_orders.track_view(session, order)


@router.post("/ping")
async def ping(
    ctx: PartnerContext = Depends(current_partner), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    await session.commit()
    return {"ok": True, "partner": {"id": str(ctx.partner.id), "name": ctx.partner.name}}


@router.post("/webhook-test")
async def webhook_test(
    ctx: PartnerContext = Depends(current_partner), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    answer = await partners.send_ping(session, ctx.partner)
    await session.commit()
    return answer


async def _json_body(request: Request) -> object:
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise ApiError(413, "payload_too_large", "Body too large")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise ApiError(400, "validation_error", "Invalid JSON") from None


@router.put("/catalog")
async def put_catalog(
    request: Request,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    answer = await vitrine_catalog.receive(session, ctx.partner, await _json_body(request))
    await session.commit()
    return answer


@router.post("/vitrine-orders/{vitrine_ref}/missing-items")
async def missing_items(
    vitrine_ref: str,
    request: Request,
    ctx: PartnerContext = Depends(current_partner),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    answer = await vitrine_orders.report_missing(session, ctx.partner, vitrine_ref, await _json_body(request))
    await session.commit()
    return answer
