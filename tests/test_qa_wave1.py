"""QA campaign 06/10 — fixes of the major bugs and the owner's rules (wave 1).

B4 accept only the price the customer saw · B26 courier resale after the purchase · B34 « Client ne
répond pas » only at the door · B43 QA hot deals hidden from real customers · B44 no buy-back."""

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Courier, HotDeal, NoResponseCase, Order
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, OrderWorld, notifications, reload, rows, set_live


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.buyer = await factory.user(email="buyer@example.test", full_name="Salma", phone_e164="+21698111222")
    return w


async def fn(client, user, name: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/api/functions/{name}", json=body, headers=auth(user))


# ─────────────────────────── B4 ───────────────────────────


async def test_accept_refuses_a_price_the_customer_did_not_see(client, world):
    order = await world.order(status="offers_received")
    offer = await world.offer(order, fee="9")
    body = {"order_id": str(order.id), "offer_id": str(offer.id)}
    r = await fn(client, world.customer, "acceptOrderOffer", {**body, "expected_fee": 6})
    assert r.status_code == 409 and r.json() == {"error": "offer_price_changed", "fee": 9.0}
    assert (await reload(Order, order.id)).status == "offers_received"
    r = await fn(client, world.customer, "acceptOrderOffer", {**body, "expected_fee": "9.000"})
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).delivery_fee == Decimal("9.000")


# ─────────────────────────── B34 ───────────────────────────


async def report(client, world, order):
    return await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"action": "report_no_response", "order_id": str(order.id)},
    )


async def test_no_response_only_within_300_m_of_the_address(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="25")
    # last position: at the shop, ~1.2 km away
    r = await report(client, world, order)
    assert r.status_code == 409 and r.json()["error"] == "too_far" and r.json()["max_m"] == 300
    assert r.json()["distance_m"] > 1000
    assert await rows(select(NoResponseCase)) == []
    # at the door (live tracking of this order)
    await set_live(order, world.courier, *SOUSSE_HOME)
    r = await report(client, world, order)
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).status == "client_no_response"


async def test_no_response_needs_a_fresh_position(client, world, factory):
    user = await factory.user(email="nogps@example.test", profile=False)
    courier = await world.make_courier(user, at=None, last_seen_at=None, phone_e164="+21655000111")
    order = await world.order(status="on_the_way", courier=courier, fee="5", purchase="25")
    r = await fn(
        client, user, "triggerEmergencyContact", {"action": "report_no_response", "order_id": str(order.id)}
    )
    assert r.status_code == 409 and r.json()["error"] == "position_unknown"


# ─────────────────────────── B26 ───────────────────────────


async def test_courier_resells_after_the_purchase_without_blaming_the_customer(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="25")
    plain = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert plain.status_code == 409  # without a no-response case, only as the courier's choice
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "courier_choice": True}
    )
    assert r.status_code == 200, r.text
    closed = await reload(Order, order.id)
    assert closed.status == "cancelled" and closed.cancel_reason == "courier_resale"
    assert await rows(select(NoResponseCase)) == []  # no incident for the customer
    assert (await reload(Courier, world.courier.id)).late_cancellations == 1
    [note] = await notifications(world.customer, "order_cancelled")
    assert (
        note.title_fr == "⚠️ Le livreur ne peut pas terminer la livraison" and note.data["fault_free"] is True
    )
    deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert deal.status == "available" and deal.purchase_amount == Decimal("25.000")


async def test_courier_choice_only_after_the_purchase(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5", purchase="25")
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "courier_choice": True}
    )
    assert r.status_code == 409 and r.json()["error"] == "order_not_resellable"


async def test_cancellation_policy_after_the_purchase(client, world):
    order = await world.order(status="purchased", courier=world.courier, fee="5", purchase="25")
    r = await fn(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(order.id), "actor": "courier"}
    )
    assert r.status_code == 200 and r.json()["policy"]["can_cancel"] is False
    assert r.json()["policy"]["after_purchase"] is True
    # B25: the customer can't cancel once the courier is at the shop, and no « frais » are promised
    r = await fn(client, world.customer, "getCancellationPolicy", {"order_id": str(order.id)})
    assert r.json()["policy"]["can_cancel"] is False and "frais" not in r.json()["policy"]["message_fr"]


# ─────────────────────────── B43 / B44 ───────────────────────────


async def resale(world, items: str, original_customer=None) -> HotDeal:
    source = await world.order(
        original_customer or world.customer,
        status="cancelled",
        courier=world.courier,
        purchase="20",
        items=items,
    )
    deal = HotDeal(
        original_order_id=source.id, courier_id=world.courier.id, items_text=items,
        purchase_amount=Decimal("20"),
        discount_percentage=Decimal("10"), price=Decimal("18"), delivery_fee=Decimal("4"),
        pickup_location=f"SRID=4326;POINT({SOUSSE_SHOP[1]} {SOUSSE_SHOP[0]})",
        expires_at=source.created_at.replace(year=source.created_at.year + 1),
    )  # fmt: skip
    from app.db import SessionLocal

    async with SessionLocal() as s:
        s.add(deal)
        await s.commit()
        await s.refresh(deal)
    return deal


async def test_qa_hot_deals_hidden_from_real_customers(client, world, monkeypatch):
    qa = await resale(world, "QA TEST 2 baguettes")
    real = await resale(world, "Pain x3")
    listed = (await fn(client, world.buyer, "listHotDeals", {"radius_km": 50})).json()["deals"]
    assert {d["id"] for d in listed} == {str(real.id)}
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(qa.id), "delivery_address": "Rue X"}
    )
    assert r.status_code == 404
    hidden = await client.get(f"/api/entities/ResaleOrder/{qa.id}", headers=auth(world.buyer))
    assert hidden.status_code == 404
    # a QA account still sees it
    monkeypatch.setattr(settings, "QA_ACCOUNTS", [world.buyer.email])
    listed = (await fn(client, world.buyer, "listHotDeals", {"radius_km": 50})).json()["deals"]
    assert str(qa.id) in {d["id"] for d in listed}


async def test_the_customer_cannot_buy_back_his_own_order(client, world):
    deal = await resale(world, "Pain x3", original_customer=world.buyer)
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deal.id), "delivery_address": "Rue X"}
    )
    assert r.status_code == 403 and r.json()["error"] == "own_order_resale"
