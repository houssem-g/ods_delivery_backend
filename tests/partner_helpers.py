"""Partner (restaurant ODS) test data: a partner created through the admin function, request bodies,
the captured webhook calls."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from app.db import SessionLocal
from app.models import NoResponseCase, Order, Partner, User
from app.services import partner_webhooks
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, reload

PARTNER_API = "/api/partner/v1"
WEBHOOK_URL = "http://partner.test/ods/webhook"


@dataclass
class PartnerWorld:
    partner: Partner
    api_key: str
    webhook_secret: str
    technical_user_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Partner-Key": self.api_key}


async def make_partner(client, admin, name: str = "Coffee and Tea", **payload: Any) -> PartnerWorld:
    body = {"name": name, "ods_restaurant_id": 1, **payload}
    response = await client.post("/api/functions/createPartner", json=body, headers=auth(admin))
    assert response.status_code == 200, response.text
    data = response.json()
    partner = await reload(Partner, data["partner"]["id"])
    return PartnerWorld(
        partner=partner,
        api_key=data["api_key"],
        webhook_secret=data["webhook_secret"],
        technical_user_id=data["partner"]["technical_user_id"],
    )


def order_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "partner_ref": "ods-1-4321",
        "test": False,
        "restaurant": {
            "name": "Coffee and Tea",
            "address": "Av. Habib Bourguiba, Sousse",
            "lat": SOUSSE_SHOP[0],
            "lng": SOUSSE_SHOP[1],
            "phone": "73123456",
            "governorate": "Sousse",
            "city": "Sousse",
        },
        "items": [
            {"name": "Café crème", "qty": 2, "unit_price": 3.5},
            {"name": "Croissant", "qty": 1, "unit_price": 1.8},
        ],
        "items_total": 8.8,
        "currency": "TND",
        "goods_payment": "courier_advances",
        "customer": {"first_name": "Amel", "phone": "22 111 222"},
        "dropoff": {
            "address": "Rue de la plage 12",
            "details": "3e étage",
            "lat": SOUSSE_HOME[0],
            "lng": SOUSSE_HOME[1],
            "governorate": "Sousse",
            "city": "Sousse",
        },
        "notes": "Sonner deux fois",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return body


async def post_order(
    client, pw: PartnerWorld, body: dict[str, Any] | None = None, **headers: str
) -> httpx.Response:
    return await client.post(
        f"{PARTNER_API}/orders", json=body or order_body(), headers={**pw.headers, **headers}
    )


@dataclass
class Captured:
    request: httpx.Request
    body: dict[str, Any]

    @property
    def headers(self) -> httpx.Headers:
        return self.request.headers


@dataclass
class WebhookServer:
    """The partner's receiving server: captures every call, answers `status` (changeable)."""

    status: int = 200
    calls: list[Captured] = field(default_factory=list)
    # « Je viens chercher » / Vitrine creation (vitrine_sync): "timeout" = no answer in time (ODS slow, or the
    # answer lost after ODS created the order); "not_created" = 200 {ok: false} (ODS failed on its side)
    mode: str | None = None

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.read()
            self.calls.append(Captured(request=request, body=json.loads(raw) if raw else {}))
            if self.status >= 600:
                raise httpx.ConnectError("refused", request=request)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("no answer", request=request)
            if self.mode == "not_created":
                return httpx.Response(200, json={"ok": False, "error": "internal"})
            # ODS answers the order it created / found again (vitrine.order_confirmed needs it)
            return httpx.Response(self.status, json={"ok": self.status < 300, "order_id": 4242})

        return httpx.MockTransport(handler)

    def install(self) -> "WebhookServer":
        partner_webhooks.set_transport(self.transport())
        return self

    def events(self) -> list[str]:
        return [c.body.get("event") for c in self.calls]

    def verify(self, call: Captured, secret: str) -> bool:
        return partner_webhooks.verify(
            secret,
            call.headers.get(partner_webhooks.TIMESTAMP_HEADER),
            call.request.read(),
            call.headers.get(partner_webhooks.SIGNATURE_HEADER),
        )


async def partner_order(world, pw: PartnerWorld, phone: str = "+21622111222", **fields: Any) -> Order:
    """A partner order written straight to the database (OrderWorld.order on the technical user)."""
    fields.setdefault("items", "[Restaurant ODS] Coffee and Tea\n1× Café — 3.500 DT")
    ref = fields.pop("partner_ref", f"ods-1-{uuid.uuid4().hex[:8]}")
    tech = await reload(User, pw.technical_user_id)
    order = await world.order(
        tech,
        source="partner",
        partner_id=pw.partner.id,
        partner_ref=ref,
        tracking_token=f"tok-{ref}",
        **fields,
    )
    if phone != order.contact_phone_e164:
        async with SessionLocal() as s:
            await s.execute(
                text("UPDATE orders SET contact_phone_e164 = :p WHERE id = :id"), {"p": phone, "id": order.id}
            )
            await s.commit()
        order = await reload(Order, order.id)
    return order


async def incident(
    order: Order, *, status: str = "resolved", resolution: str | None = "cancelled_kept"
) -> None:
    """A counted « client ne répond pas » incident on the order (its deadline 7 minutes ago)."""
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                courier_id=order.courier_id,
                status=status,
                resolution=resolution,
                resolved_at=now - timedelta(minutes=7) if status == "resolved" else None,
                purchase_amount=order.purchase_amount or 0,
                started_at=now - timedelta(minutes=10),
                deadline_at=now - timedelta(minutes=7),
                final_at=now - timedelta(minutes=7),
                incident_counted=True,
                channels={},
                messaging_status="whatsapp_disabled",
            )
        )
        await s.commit()
