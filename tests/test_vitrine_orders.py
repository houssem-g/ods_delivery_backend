"""Vitrine orders (étape 8): the cart priced again server side, the refusals of the contract §5, the
QA path, the signed events to the linked ODS restaurant (§7) and its « plat manquant » (§7.3)."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal, transaction
from app.models import Order, OrderOffer, OrderStockCheck, OrderStop, Partner, PartnerWebhookDelivery
from app.services import partner_webhooks
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, WebhookServer, make_partner
from tests.vitrine_helpers import (
    QA_COURIER,
    QA_CUSTOMER,
    cart_order,
    catalog_body,
    fn,
    manual_vitrine,
    ok,
    shop_body,
)


@pytest.fixture(autouse=True)
def _ods_answers():
    """ODS answers « ok, order n° … »: a Vitrine order is created there synchronously, inside the
    acceptance (app/services/vitrine_sync.py). A test installs its own server to look at the calls."""
    from tests.partner_helpers import WebhookServer

    return WebhookServer().install()


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
async def qa(factory):
    return await factory.user(email=QA_CUSTOMER, full_name="Lemel Qa Client", phone_e164="+21622111333")


@pytest.fixture
async def qa_courier(factory, world):
    user = await factory.user(email=QA_COURIER, full_name="Vovine", profile=False)
    await world.make_courier(user, display_name="Vovine Livreur", phone_e164="+21655999888")
    return user


async def public_vitrine(client, admin, **kw):
    shop_id, ids = await manual_vitrine(client, admin, **kw)
    await ok(client, admin, "setVitrineSettings", {"public_allowed": True})
    await ok(client, admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"})
    return shop_id, ids


async def place(client, user, body):
    return await fn(client, user, "placeOrder", body)


async def deliveries(order_id=None) -> list[PartnerWebhookDelivery]:
    # in queueing order (created_at is the transaction's start time: equal within one transaction)
    stmt = select(PartnerWebhookDelivery).order_by(
        PartnerWebhookDelivery.created_at, PartnerWebhookDelivery.next_attempt_at, PartnerWebhookDelivery.id
    )
    if order_id is not None:
        stmt = stmt.where(PartnerWebhookDelivery.order_id == order_id)
    return await rows(stmt)


async def run_job() -> dict:
    async with transaction() as session:
        return await partner_webhooks.deliver_due(session)


async def step(client, courier_user, order_id, body):
    r = await client.patch(f"/api/entities/Order/{order_id}", json=body, headers=auth(courier_user))
    assert r.status_code == 200, r.text


async def accepted(client, customer, courier_user, order_id, fee=4):
    offer = await ok(
        client, courier_user, "createOrderOffer", {"order_id": order_id, "fee": fee, "eta_minutes": 15}
    )
    await ok(client, customer, "acceptOrderOffer", {"order_id": order_id, "offer_id": offer["offer"]["id"]})


async def linked(client, world):
    """An ODS restaurant with its webhook, its catalog pushed, its Vitrine active (qa)."""
    pw = await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)
    r = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(), headers=pw.headers)
    assert r.status_code == 200, r.text
    shop_id = (
        await ok(
            client,
            world.admin,
            "createVitrine",
            {"shop": shop_body("Coffee and Tea"), "source": "ods", "partner_id": str(pw.partner.id)},
        )
    )["vitrine"]["shop_id"]
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    ids = {
        p["ods_menu_id"]: p["id"]
        for p in (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["products"]
    }
    return pw, shop_id, ids


# --- placeOrder --------------------------------------------------------------------------------------


async def test_the_cart_is_priced_again_by_the_server(client, world):
    shop_id, [cafe, croissant] = await public_vitrine(client, world.admin)
    lines = [
        {"product_id": cafe, "qty": 2, "note": "sans sucre", "unit_price": 2.5},
        {"product_id": croissant, "qty": 1},
    ]
    # whatever the client says about the items, the shop or the ceiling is ignored
    forged = {"items_text": "1× Tout gratuit", "estimated_price": 0.1, "budget_max": 0.1}
    elsewhere = {"shop_name": "Ailleurs", "shop_lat": 36.8, "shop_lng": 10.1}
    body = cart_order(shop_id, lines, **forged, **elsewhere, notes="Et une bouteille d'eau")
    r = await place(client, world.customer, body)
    assert r.status_code == 200, r.text
    order = await reload(Order, r.json()["order"]["id"])
    assert order.source == "vitrine" and str(order.vitrine_shop_id) == shop_id
    assert order.items_text == (
        "[Vitrine] Chez Amel\n2× Café crème — 5.000 DT (sans sucre)\n1× Croissant — 1.800 DT\n"
        "Total articles : 6.800 DT"
    )
    assert float(order.estimated_price) == float(order.budget_max) == 6.8 and order.quantity == 3
    assert order.notes == "Et une bouteille d'eau"
    [stop] = await rows(select(OrderStop).where(OrderStop.order_id == order.id))
    assert stop.name == "Chez Amel" and str(stop.shop_id) == shop_id
    assert order.cart["items_total"] == "6.800" and order.cart["lines"][0]["unit_price"] == "2.500"
    doc = r.json()["order"]
    assert doc["source"] == "vitrine" and doc["vitrine_ods"] is False
    assert doc["vitrine_cart"]["lines"][0] == {
        "product_id": cafe, "name": "Café crème", "qty": 2, "unit_price": "2.500", "line_total": "5.000",
        "note": "sans sucre",
    }  # fmt: skip
    # the courier bidding sees the lines and prices
    seen = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert seen["vitrine_cart"]["items_total"] == "6.800" and seen["budget_max"] == 6.8


async def test_a_forged_or_changed_price_is_never_taken(client, world):
    shop_id, [cafe, _] = await public_vitrine(client, world.admin)
    r = await place(
        client, world.customer, cart_order(shop_id, [{"product_id": cafe, "qty": 1, "unit_price": 0.1}])
    )
    assert r.status_code == 409 and r.json() == {
        "error": "prices_changed",
        "lines": [
            {"product_id": cafe, "name": "Café crème", "old_price": 0.1, "new_price": 2.5, "base_price": 2.5}
        ],
    }
    await ok(client, world.admin, "upsertVitrineProduct", {"shop_id": shop_id, "id": cafe, "price": 2.8})
    r = await place(
        client, world.customer, cart_order(shop_id, [{"product_id": cafe, "qty": 1, "unit_price": 2.5}])
    )
    assert r.status_code == 409 and r.json()["lines"][0]["new_price"] == 2.8
    assert await rows(select(Order)) == []
    r = await place(
        client, world.customer, cart_order(shop_id, [{"product_id": cafe, "qty": 1, "unit_price": 2.8}])
    )
    assert r.status_code == 200 and r.json()["order"]["estimated_price"] == 2.8


async def test_refusals(client, world, qa):
    shop_id, [cafe, croissant] = await public_vitrine(client, world.admin)
    customer = world.customer

    async def refused(lines, status, error, **extra):
        r = await place(client, customer, cart_order(shop_id, lines, **extra))
        assert r.status_code == status and r.json()["error"] == error, r.text
        return r.json()

    await refused([], 400, "invalid_cart")
    await refused([{"product_id": cafe, "qty": 0}], 400, "invalid_cart")
    await refused([{"product_id": cafe, "qty": 1}, {"product_id": cafe, "qty": 2}], 400, "invalid_cart")
    await ok(
        client, world.admin, "upsertVitrineProduct", {"shop_id": shop_id, "id": croissant, "available": False}
    )
    body = await refused(
        [{"product_id": cafe, "qty": 1}, {"product_id": croissant, "qty": 1}], 409, "items_unavailable"
    )
    assert body["product_ids"] == [croissant]
    await refused([{"product_id": str(uuid.uuid4()), "qty": 1}], 409, "items_unavailable")
    # closed today (only open on another day): « ouvre à … »
    today = datetime.now(UTC).astimezone(ZoneInfo("Africa/Tunis")).isoweekday()
    other = today % 7 + 1
    await ok(
        client,
        world.admin,
        "updateVitrine",
        {"shop_id": shop_id, "hours": {"weekly": [{"days": [other], "open": "07:00", "close": "09:00"}]}},
    )
    body = await refused([{"product_id": cafe, "qty": 1}], 409, "shop_closed")
    assert body["opens_at"] == "07:00"
    page = (await ok(client, customer, "getVitrine", {"shop_id": shop_id}))["vitrine"]
    assert page["open_now"] is False and page["ordering"] == {"allowed": False, "reason": "shop_closed"}
    # a qa Vitrine: a non-QA customer gets exactly « unknown shop »
    await ok(client, world.admin, "updateVitrine", {"shop_id": shop_id, "hours": None})
    await ok(client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "qa"})
    await refused([{"product_id": cafe, "qty": 1}], 404, "not_found")
    assert await rows(select(Order)) == []
    r = await place(client, qa, cart_order(shop_id, [{"product_id": cafe, "qty": 1}]))
    assert r.status_code == 200, r.text


async def test_a_qa_vitrine_order_reaches_the_qa_couriers_only(client, world, qa, qa_courier):
    shop_id, [cafe, _] = await manual_vitrine(client, world.admin)
    r = await place(client, qa, cart_order(shop_id, [{"product_id": cafe, "qty": 1}]))
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] == 1
    order = await reload(Order, r.json()["order"]["id"])
    assert order.items_text.startswith("QA TEST [Vitrine] Chez Amel\n1× Café crème — 2.500 DT")
    assert [n.order_id for n in await notifications(qa_courier, "new_order")] == [order.id]
    assert await notifications(world.courier_user, "new_order") == []
    assert (
        await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))
    ).status_code == 404


async def test_a_restaurant_in_debt_takes_no_vitrine_order(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    async with SessionLocal() as s:
        partner = await s.get(Partner, pw.partner.id)
        partner.debt_amount = 12.5
        await s.commit()
    r = await place(client, qa, cart_order(shop_id, [{"product_id": ids[345], "qty": 1}]))
    assert r.status_code == 409 and r.json() == {"error": "restaurant_debt_unpaid", "amount": 12.5}


# --- the ODS restaurant hears about it ---------------------------------------------------------------------


async def test_an_accepted_offer_sends_the_order_to_ods_then_every_step(client, world, qa, qa_courier):
    server = WebhookServer().install()
    pw, shop_id, ids = await linked(client, world)
    lines = [{"product_id": ids[345], "qty": 2, "note": "bien chaud"}, {"product_id": ids[346], "qty": 1}]
    order_id = await place(client, qa, cart_order(shop_id, lines, notes="Une serviette"))
    assert order_id.status_code == 200, order_id.text
    assert order_id.json()["order"]["vitrine_ods"] is True
    order_id = order_id.json()["order"]["id"]
    await ok(client, qa_courier, "createOrderOffer", {"order_id": order_id, "fee": 4, "eta_minutes": 15})
    assert await deliveries() == []  # before the acceptance: ODS knows nothing (no cooking without a courier)
    offer_id = (await rows(select(OrderOffer)))[0].id
    await ok(client, qa, "acceptOrderOffer", {"order_id": order_id, "offer_id": str(offer_id)})

    [confirmed] = await deliveries()
    assert confirmed.event == "vitrine.order_confirmed" and confirmed.partner_id == pw.partner.id
    data = confirmed.payload["data"]
    assert data["vitrine_ref"] == order_id and data["shop_id"] == shop_id
    assert data["lines"] == [
        {"ods_menu_id": 345, "name": "Café crème", "qty": 2, "unit_price": 2.5, "note": "bien chaud"},
        {"ods_menu_id": 346, "name": "Thé à la menthe", "qty": 1, "unit_price": 1.5, "note": None},
    ]
    assert data["items_total"] == 6.5 and data["extra_text"] == "Une serviette"
    assert data["customer"] == {"first_name": "Lemel"} and data["pay_at_counter"] is True
    assert data["test"] is True and data["delivery_fee"] == 4
    assert data["courier"] == {**data["courier"], "first_name": "Vovine", "phone": "+21655999888"}

    # sent inside the acceptance (vitrine_sync), never queued
    assert confirmed.status == "delivered" and await run_job() == {"due": 0, "delivered": 0, "failed": 0}
    [call] = server.calls
    assert (
        server.verify(call, pw.webhook_secret) and call.headers["X-ODSD-Event"] == "vitrine.order_confirmed"
    )
    raw = call.request.read().decode()
    for secret in ("22111333", "Rue de la plage", "Qa Client", QA_CUSTOMER):
        assert secret not in raw  # no customer data: first name only
    assert call.body["data"]["order"]["courier_state"] == "assigned" and call.body["partner_ref"] is None

    await step(client, qa_courier, order_id, {"status": "at_shop"})
    await step(client, qa_courier, order_id, {"status": "purchased", "purchase_amount": 6.5})
    await step(client, qa_courier, order_id, {"status": "on_the_way"})
    await step(client, qa_courier, order_id, {"status": "delivered"})
    updates = [(d.event, d.payload["data"].get("courier_state")) for d in await deliveries()]
    assert updates == [
        ("vitrine.order_confirmed", None),
        ("vitrine.order_updated", "at_restaurant"),
        ("vitrine.order_updated", "picked_up"),
        ("vitrine.order_updated", "on_the_way"),
        ("vitrine.order_updated", "delivered"),
    ]
    picked = (await deliveries())[2]
    assert picked.payload["data"]["purchase_amount"] == 6.5

    # a refused delivery is retried with the same X-ODSD-Delivery-Id (the receiver deduplicates)
    server.status = 500
    await run_job()
    server.status = 200
    async with SessionLocal() as s:
        await s.execute(text("UPDATE partner_webhook_deliveries SET next_attempt_at = now()"))
        await s.commit()
    await run_job()
    ids_sent = [c.headers["X-ODSD-Delivery-Id"] for c in server.calls[1:]]
    assert len(ids_sent) == 8 and len(set(ids_sent)) == 4


async def test_cancellation_and_courier_drop_reach_ods(client, world, qa, qa_courier):
    _pw, shop_id, ids = await linked(client, world)
    order_id = (await place(client, qa, cart_order(shop_id, [{"product_id": ids[345], "qty": 1}]))).json()[
        "order"
    ]["id"]
    await accepted(client, qa, qa_courier, order_id)
    # the courier gives it up: back to the pool, the ODS order stays valid
    r = await fn(
        client,
        qa_courier,
        "cancelOrder",
        {
            "order_id": order_id,
            "reason": "panne",
            "cancelled_by": "courier",
            "courier_id": str((await reload(Order, order_id)).courier_id),
        },
    )
    assert r.status_code == 200, r.text
    assert (await reload(Order, order_id)).status == "pending"
    r = await fn(
        client,
        qa,
        "cancelOrder",
        {"order_id": order_id, "reason": "changed_mind", "cancelled_by": "customer"},
    )
    assert r.status_code == 200, r.text
    events = [
        (d.event, d.payload["data"].get("courier_state"), d.payload["data"].get("stage"))
        for d in await deliveries()
    ]
    assert events == [
        ("vitrine.order_confirmed", None, None),
        ("vitrine.order_updated", "searching", None),
        ("vitrine.order_cancelled", None, "searching"),
    ]
    cancelled = (await deliveries())[-1].payload["data"]
    assert cancelled["cancelled_by"] == "customer" and cancelled["reason"] == "changed_mind"


async def test_a_manual_vitrine_sends_no_webhook(client, world, qa, qa_courier):
    shop_id, [cafe, _] = await manual_vitrine(client, world.admin)
    order_id = (await place(client, qa, cart_order(shop_id, [{"product_id": cafe, "qty": 1}]))).json()[
        "order"
    ]["id"]
    await accepted(client, qa, qa_courier, order_id)
    await step(client, qa_courier, order_id, {"status": "at_shop"})
    assert await deliveries() == []


async def test_a_revoked_restaurant_gets_nothing_new(client, world, qa, qa_courier):
    pw, shop_id, ids = await linked(client, world)
    order_id = (await place(client, qa, cart_order(shop_id, [{"product_id": ids[345], "qty": 1}]))).json()[
        "order"
    ]["id"]
    await ok(client, world.admin, "revokePartner", {"partner_id": str(pw.partner.id)})
    await accepted(client, qa, qa_courier, order_id)
    assert await deliveries() == []  # the order goes on as a classic Delivery order


# --- missing-items --------------------------------------------------------------------------------------


async def test_the_restaurant_reports_a_missing_dish(client, world, qa, qa_courier):
    pw, shop_id, ids = await linked(client, world)
    lines = [{"product_id": ids[345], "qty": 2}, {"product_id": ids[346], "qty": 1}]
    order_id = (await place(client, qa, cart_order(shop_id, lines))).json()["order"]["id"]
    url = f"{PARTNER_API}/vitrine-orders/{order_id}/missing-items"
    # not confirmed yet: unknown to the restaurant
    assert (await client.post(url, json={"ods_menu_ids": [345]}, headers=pw.headers)).status_code == 404
    await accepted(client, qa, qa_courier, order_id)
    other = await make_partner(client, world.admin, name="Autre")
    assert (await client.post(url, json={"ods_menu_ids": [345]}, headers=other.headers)).status_code == 404
    assert (await client.post(url, json={"ods_menu_ids": [999]}, headers=pw.headers)).status_code == 400

    r = await client.post(url, json={"ods_menu_ids": [345]}, headers=pw.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "asked" and r.json()["stock_check"]["deadline_at"].endswith("Z")
    order = await reload(Order, order_id)
    assert order.status == "price_confirmation_needed"
    [check] = await rows(select(OrderStockCheck))
    assert check.ods_menu_ids == [345] and check.missing_text == "Le restaurant n'a plus : Café crème"
    assert check.nothing_available is False and float(check.missing_price) == 5.0
    [note] = await notifications(qa, "stock_check")
    assert note.title_fr == "🛒 Un article manque chez Coffee and Tea"
    again = await client.post(url, json={"ods_menu_ids": [345]}, headers=pw.headers)
    assert again.status_code == 409 and again.json()["error"] == "stock_check_pending"

    # the customer removes it: the order goes back where it was, ODS is told
    await ok(
        client,
        qa,
        "answerStockCheck",
        {"order_id": order_id, "stock_check_id": str(check.id), "decision": "skip"},
    )
    assert (await reload(Order, order_id)).status == "accepted"
    last = (await deliveries())[-1]
    assert last.event == "vitrine.order_updated"
    assert last.payload["data"]["missing_items"] == {"decision": "removed", "ods_menu_ids": [345]}
    dup = await client.post(url, json={"ods_menu_ids": [345]}, headers=pw.headers)
    assert dup.status_code == 409 and dup.json()["error"] == "item_already_reported"

    # once the courier paid at the counter: too late
    await step(client, qa_courier, order_id, {"status": "at_shop"})
    await step(client, qa_courier, order_id, {"status": "purchased", "purchase_amount": 1.5})
    late = await client.post(url, json={"ods_menu_ids": [346]}, headers=pw.headers)
    assert late.status_code == 409 and late.json()["error"] == "already_purchased"
    states = [
        d.payload["data"].get("courier_state")
        for d in await deliveries()
        if d.event == "vitrine.order_updated"
    ]
    assert states == ["assigned", "at_restaurant", "picked_up"]  # « assigned » = the missing-items update


async def test_customer_absent_and_meal_returned_reaches_ods(client, world, qa, qa_courier):
    """Default decision (to confirm by the owner): the classic Delivery rules apply; when the courier
    brings the meal back, ODS hears « returned » then the cancellation (no return trip owed)."""
    from app.services import vitrine_orders

    _pw, shop_id, ids = await linked(client, world)
    order_id = (await place(client, qa, cart_order(shop_id, [{"product_id": ids[345], "qty": 1}]))).json()[
        "order"
    ]["id"]
    await accepted(client, qa, qa_courier, order_id)
    async with transaction() as session:
        order = await session.get(Order, uuid.UUID(order_id))
        await vitrine_orders.on_transition(session, order, "on_the_way", "client_no_response", None, None)
        await vitrine_orders.on_transition(
            session, order, "client_no_response", "cancelled", "goods_returned_to_shop", "courier"
        )
    events = [(d.event, d.payload["data"].get("courier_state")) for d in await deliveries()]
    assert events[1:] == [
        ("vitrine.order_updated", "incident_no_response"),
        ("vitrine.order_updated", "returned"),
        ("vitrine.order_cancelled", None),
    ]
    assert (await deliveries())[-1].payload["data"]["stage"] == "incident_no_response"
