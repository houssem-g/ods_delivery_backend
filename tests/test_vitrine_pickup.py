"""« Je viens chercher » (click and collect, vcc 18/10/2026): offered only when ODS says so, the order goes
straight to ODS (never to a courier), the restaurant moves it by hand (préparation, prête, payée, refusée,
pas venu, prévenir), the badge, the customer's cancellation, the confirmation that never arrives."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal, transaction
from app.models import Order, PartnerWebhookDelivery
from app.services import partner_webhooks, vitrine_pickup
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, WebhookServer, make_partner
from tests.vitrine_helpers import QA_CUSTOMER, cart_order, catalog_body, fn, ok, shop_body


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
async def qa(factory):
    return await factory.user(email=QA_CUSTOMER, full_name="Lemel Qa Client", phone_e164="+21622111333")


def pickup_catalog(enabled: bool = True, prep: int | None = 15, **kw):
    body = catalog_body(**kw)
    body["restaurant"]["pickup"] = {"enabled": enabled, **({"prep_minutes": prep} if prep else {})}
    return body


async def linked(client, world, *, pickup: bool | None = True, n: int = 1):
    pw = await make_partner(
        client, world.admin, f"Coffee and Tea {n}", webhook_url=WEBHOOK_URL, ods_restaurant_id=n
    )
    body = catalog_body() if pickup is None else pickup_catalog(pickup)
    r = await client.put(f"{PARTNER_API}/catalog", json=body, headers=pw.headers)
    assert r.status_code == 200, r.text
    shop_id = (
        await ok(
            client,
            world.admin,
            "createVitrine",
            {"shop": shop_body(f"Coffee and Tea {n}"), "source": "ods", "partner_id": str(pw.partner.id)},
        )
    )["vitrine"]["shop_id"]
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    ids = {
        p["ods_menu_id"]: p["id"]
        for p in (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["products"]
    }
    return pw, shop_id, ids


def pickup_order(shop_id, lines, **vitrine):
    return cart_order(shop_id, lines, vitrine={"fulfillment": "pickup", **vitrine})


async def place(client, user, body):
    return await fn(client, user, "placeOrder", body)


async def deliveries(order_id=None) -> list[PartnerWebhookDelivery]:
    stmt = select(PartnerWebhookDelivery).order_by(
        PartnerWebhookDelivery.created_at, PartnerWebhookDelivery.next_attempt_at, PartnerWebhookDelivery.id
    )
    if order_id is not None:
        stmt = stmt.where(PartnerWebhookDelivery.order_id == order_id)
    return await rows(stmt)


async def run_job() -> dict:
    async with transaction() as session:
        return await partner_webhooks.deliver_due(session)


async def state(client, pw, ref, body):
    return await client.post(f"{PARTNER_API}/vitrine-orders/{ref}/pickup", json=body, headers=pw.headers)


async def placed(client, qa, shop_id, ids, **kw) -> str:
    r = await place(client, qa, pickup_order(shop_id, [{"product_id": ids[345], "qty": 2}], **kw))
    assert r.status_code == 200, r.text
    return r.json()["order"]["id"]


# --- offered or not -------------------------------------------------------------------------------------


async def test_offered_only_when_the_ods_catalog_says_so(client, world, qa):
    _pw, shop_id, ids = await linked(client, world, pickup=None)  # an older ODS: no `pickup` key
    vit = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]
    assert vit["pickup"] == {"available": False, "reason": "off", "prep_minutes": None, "last_at": None}
    r = await place(client, qa, pickup_order(shop_id, [{"product_id": ids[345], "qty": 1}]))
    assert r.status_code == 409 and r.json()["error"] == "pickup_unavailable"

    pw2, shop2, ids2 = await linked(client, world, n=2)
    vit = (await ok(client, qa, "getVitrine", {"shop_id": shop2}))["vitrine"]
    assert vit["pickup"]["available"] is True and vit["pickup"]["prep_minutes"] == 15

    # « À emporter » switched off in ODS: the next catalog says so
    r = await client.put(
        f"{PARTNER_API}/catalog", json=pickup_catalog(False, version="v2"), headers=pw2.headers
    )
    assert r.status_code == 200 and r.json()["status"] == "updated"
    vit = (await ok(client, qa, "getVitrine", {"shop_id": shop2}))["vitrine"]
    assert vit["pickup"]["available"] is False and vit["pickup"]["reason"] == "off"
    r = await place(client, qa, pickup_order(shop2, [{"product_id": ids2[345], "qty": 1}]))
    assert r.status_code == 409 and r.json()["error"] == "pickup_unavailable"


async def test_a_manual_vitrine_never_offers_it(client, world, qa):
    from tests.vitrine_helpers import manual_vitrine

    shop_id, [cafe, _] = await manual_vitrine(client, world.admin)
    vit = (await ok(client, world.admin, "getVitrine", {"shop_id": shop_id}))["vitrine"]
    assert vit["pickup"]["available"] is False and vit["pickup"]["reason"] == "not_linked"
    r = await place(client, world.admin, pickup_order(shop_id, [{"product_id": cafe, "qty": 1}]))
    assert r.status_code == 409 and r.json()["error"] == "pickup_unavailable"


async def test_the_catalog_answer_gives_ods_its_vitrine_link(client, world):
    pw, shop_id, _ = await linked(client, world)
    r = await client.put(f"{PARTNER_API}/catalog", json=pickup_catalog(version="v9"), headers=pw.headers)
    vitrine = r.json()["vitrine"]
    assert vitrine["shop_id"] == shop_id and vitrine["status"] == "active" and vitrine["public"] is False
    assert vitrine["slug"]
    # a catalog without `pickup` keeps its old fingerprint (an ODS that never sends it is « unchanged »)
    r1 = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(version="a"), headers=pw.headers)
    r2 = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(version="b"), headers=pw.headers)
    assert r1.json()["status"] == "updated" and r2.json()["status"] == "unchanged"


def test_closing_soon_and_closed():
    # 21:50 Tunis on a shop closing at 22:00: too late; at 21:00 the last minute is 21:45
    tunis = datetime(2026, 10, 18, 20, 50, tzinfo=UTC)  # 21:50 in Tunis (UTC+1)
    hours = {
        "tz": "Africa/Tunis",
        "weekly": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "07:00", "close": "22:00"}],
    }
    from app.services.vitrine import open_state

    st = open_state(hours, tunis)
    assert st["open_now"] is True and st["closes_at"] == "22:00"
    assert vitrine_pickup._minutes_until("22:00", tunis) == 10
    assert vitrine_pickup._minus("22:00", 15) == "21:45"
    assert vitrine_pickup._minutes_until("22:00", tunis - timedelta(minutes=50)) == 60


# --- placing --------------------------------------------------------------------------------------------


async def test_a_pickup_order_goes_to_ods_and_never_to_a_courier(client, world, qa):
    server = WebhookServer().install()
    _pw, shop_id, ids = await linked(client, world)
    body = pickup_order(
        shop_id, [{"product_id": ids[345], "qty": 2, "note": "bien chaud"}], pickup_name="Maman de Sami"
    )
    body["order"]["notes"] = "Une serviette"
    r = await place(client, qa, body)
    assert r.status_code == 200, r.text
    assert r.json()["dispatched"] == 0
    doc = r.json()["order"]
    assert doc["fulfillment"] == "pickup" and doc["status"] == "awaiting_restaurant"
    assert len(doc["pickup_code"]) == 3 and doc["pickup_code"].isdigit()
    assert doc["pickup_name"] == "Maman de Sami" and doc["restaurant_ack_at"] is None
    order = await reload(Order, doc["id"])
    assert order.delivery_address == "Av. Habib Bourguiba, Sousse"  # the place where it is handed over
    assert float(order.delivery_fee) == 0 and order.courier_id is None
    assert await notifications(world.courier_user) == []  # no courier is ever told

    # couriers never see it (open orders are pending / offers_received only)
    seen = await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))
    assert seen.status_code in (403, 404)

    [confirmed] = await deliveries()
    data = confirmed.payload["data"]
    assert confirmed.event == "vitrine.order_confirmed"
    assert data["fulfillment"] == "pickup" and data["pickup_code"] == doc["pickup_code"]
    assert data["courier"] is None and data["delivery_fee"] == 0 and data["pay_at_counter"] is True
    assert data["customer"] == {"first_name": "Maman de Sami", "badge": None}
    assert data["lines"][0]["ods_menu_id"] == 345 and data["items_total"] == 5.0
    assert data["extra_text"] == "Une serviette"

    assert await run_job() == {"due": 1, "delivered": 1, "failed": 0}
    raw = server.calls[0].request.read().decode()
    for secret in ("22111333", "Rue de la plage", "Qa Client", QA_CUSTOMER):
        assert secret not in raw
    assert server.calls[0].body["data"]["order"]["fulfillment"] == "pickup"
    assert (await reload(Order, order.id)).restaurant_ack_at is not None


async def test_refusals_double_order_and_two_open_at_most(client, world, qa):
    _pw, shop_id, ids = await linked(client, world)
    first = await placed(client, qa, shop_id, ids)
    # the same cart again within 10 min: « vous avez déjà envoyé cette commande »
    r = await place(client, qa, pickup_order(shop_id, [{"product_id": ids[345], "qty": 2}]))
    assert r.status_code == 409 and r.json()["error"] == "duplicate_pickup" and r.json()["order_id"] == first
    # sent anyway (for someone else)
    await placed(client, qa, shop_id, ids, allow_duplicate=True)
    r = await place(client, qa, pickup_order(shop_id, [{"product_id": ids[346], "qty": 1}]))
    assert r.status_code == 409 and r.json()["error"] == "too_many_pickups" and r.json()["max"] == 2
    bad = cart_order(shop_id, [{"product_id": ids[346], "qty": 1}], vitrine={"fulfillment": "drone"})
    r = await place(client, qa, bad)
    assert r.status_code == 400 and r.json()["error"] == "invalid_cart"
    # two different codes the same day
    codes = {o.pickup_code for o in await rows(select(Order))}
    assert len(codes) == 2


# --- the restaurant's steps --------------------------------------------------------------------------------


async def test_received_preparing_ready_collected(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    r = await state(client, pw, ref, {"state": "preparing"})
    assert r.status_code == 200 and r.json() == {"status": "updated", "order_status": "preparing"}
    assert (await state(client, pw, ref, {"state": "preparing"})).json()["status"] == "unchanged"
    [prep] = await notifications(qa, "order_preparing")
    assert "prépare votre commande" in prep.body_fr

    r = await state(client, pw, ref, {"state": "ready"})
    assert r.json()["order_status"] == "ready"
    [ready] = await notifications(qa, "pickup_ready")
    order = await reload(Order, ref)
    assert ready.title_fr == f"Commande prête · n° {order.pickup_code}"
    assert "À payer au comptoir : 5.000 DT" in ready.body_fr and order.ready_at is not None
    # a late « preparing » after « ready »: unchanged, never backwards
    assert (await state(client, pw, ref, {"state": "preparing"})).json()["status"] == "unchanged"

    # « Prévenir le client », by hand: not twice within 2 min
    r = await state(client, pw, ref, {"state": "notify"})
    assert r.json()["reason"] == "too_soon"
    async with SessionLocal() as s:
        await s.execute(text("UPDATE orders SET ready_notified_at = now() - interval '3 minutes'"))
        await s.commit()
    assert (await state(client, pw, ref, {"state": "notify"})).json()["status"] == "updated"
    assert len(await notifications(qa, "pickup_ready")) == 2

    r = await state(client, pw, ref, {"state": "collected"})
    assert r.json()["order_status"] == "collected"
    order = await reload(Order, ref)
    assert order.collected_at is not None
    # refusing or « pas venu » once paid: never
    r = await state(client, pw, ref, {"state": "not_collected"})
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"
    doc = (await client.get(f"/api/entities/Order/{ref}", headers=auth(qa))).json()
    assert doc["status"] == "collected" and doc["collected_at"]


async def test_paid_while_still_preparing(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    r = await state(client, pw, ref, {"state": "collected"})
    assert r.json()["order_status"] == "collected"


async def test_refused_before_preparing_only(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    r = await state(client, pw, ref, {"state": "refused", "reason": "nonsense"})
    assert r.status_code == 400
    r = await state(client, pw, ref, {"state": "refused", "reason": "busy"})
    assert r.json() == {"status": "updated", "order_status": "cancelled"}
    order = await reload(Order, ref)
    assert order.cancelled_by == "restaurant" and order.cancel_reason == "restaurant_refused:busy"
    [told] = await notifications(qa, "order_cancelled")
    assert "trop de commandes" in told.body_fr and "Rien à payer" in told.body_fr
    # ODS made it: no cancellation event back
    assert [d.event for d in await deliveries(order.id)] == ["vitrine.order_confirmed"]
    # a refusal never counts against the customer
    async with SessionLocal() as s:
        assert await vitrine_pickup.badge(s, qa.id) is None

    ref2 = await placed(client, qa, shop_id, ids)
    await state(client, pw, ref2, {"state": "preparing"})
    r = await state(client, pw, ref2, {"state": "refused"})
    assert r.status_code == 409 and r.json()["order_status"] == "preparing"


async def test_not_collected_counts_in_the_badge_by_hand_only(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    # « Pas venu » only on a ready order — and no minimum delay (owner 18/10: the restaurant decides)
    r = await state(client, pw, ref, {"state": "not_collected"})
    assert r.status_code == 409
    await state(client, pw, ref, {"state": "ready"})
    r = await state(client, pw, ref, {"state": "not_collected"})
    assert r.json()["order_status"] == "cancelled"
    order = await reload(Order, ref)
    assert order.cancel_reason == "not_collected" and order.cancelled_by == "restaurant"
    told = (await notifications(qa, "order_cancelled"))[-1]
    assert "vous n'êtes pas venu" in told.body_fr

    # the badge: a QA (test) order never counts; a real one does
    async with SessionLocal() as s:
        assert await vitrine_pickup.badge(s, qa.id) is None
        await s.execute(text("UPDATE orders SET items_text = replace(items_text, 'QA TEST ', '')"))
        await s.commit()
    async with SessionLocal() as s:
        assert await vitrine_pickup.badge(s, qa.id) == {"unpaid": 1, "of": 1}
    # and the next order tells the restaurant
    ref2 = await placed(client, qa, shop_id, ids)
    confirmed = (await deliveries(ref2))[0]
    assert confirmed.payload["data"]["customer"]["badge"] == {"unpaid": 1, "of": 1}


async def test_another_restaurant_cannot_touch_the_order(client, world, qa):
    _pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    other = await make_partner(
        client, world.admin, "Autre resto", webhook_url=WEBHOOK_URL, ods_restaurant_id=9
    )
    r = await state(client, other, ref, {"state": "ready"})
    assert r.status_code == 404
    r = await client.post(f"{PARTNER_API}/vitrine-orders/{ref}/pickup", json={"state": "ready"})
    assert r.status_code == 401


# --- the customer -------------------------------------------------------------------------------------------


async def test_the_customer_cancels_before_preparation_only(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    body = {"order_id": ref, "cancelled_by": "customer", "reason": "Je ne viens plus"}
    assert (await fn(client, qa, "cancelOrder", body)).status_code == 200
    order = await reload(Order, ref)
    assert order.status == "cancelled" and order.cancel_reason == "customer_cancelled"
    events = [(d.event, d.payload["data"].get("reason")) for d in await deliveries(order.id)]
    assert events == [("vitrine.order_confirmed", None), ("vitrine.order_cancelled", "customer_cancelled")]

    ref2 = await placed(client, qa, shop_id, ids)
    await state(client, pw, ref2, {"state": "preparing"})
    r = await fn(client, qa, "cancelOrder", {**body, "order_id": ref2})
    assert r.status_code == 409 and r.json()["error"] == "pickup_in_preparation"


async def test_ods_never_received_it(client, world, qa):
    server = WebhookServer().install()
    server.status = 500
    _pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    for _ in range(partner_webhooks.MAX_ATTEMPTS):
        await run_job()
        async with SessionLocal() as s:
            await s.execute(text("UPDATE partner_webhook_deliveries SET next_attempt_at = now()"))
            await s.commit()
    order = await reload(Order, ref)
    assert order.status == "cancelled" and order.cancel_reason == "restaurant_unreachable"
    told = (await notifications(qa, "order_cancelled"))[-1]
    assert "n'a pas reçu votre commande" in told.body_fr


async def test_cancelled_at_the_till_from_any_step_never_counts(client, world, qa):
    pw, shop_id, ids = await linked(client, world)
    ref = await placed(client, qa, shop_id, ids)
    await state(client, pw, ref, {"state": "ready"})
    r = await state(client, pw, ref, {"state": "cancelled"})
    assert r.json() == {"status": "updated", "order_status": "cancelled"}
    order = await reload(Order, ref)
    assert order.cancel_reason == "restaurant_cancelled" and order.cancelled_by == "restaurant"
    told = (await notifications(qa, "order_cancelled"))[-1]
    assert "a annulé votre commande" in told.body_fr
    async with SessionLocal() as s:
        await s.execute(text("UPDATE orders SET items_text = replace(items_text, 'QA TEST ', '')"))
        await s.commit()
    async with SessionLocal() as s:
        assert await vitrine_pickup.badge(s, qa.id) is None


async def test_a_forgotten_order_of_yesterday_never_locks_the_customer_out(client, world, qa):
    _pw, shop_id, ids = await linked(client, world)
    await placed(client, qa, shop_id, ids)
    await placed(client, qa, shop_id, ids, allow_duplicate=True)
    async with SessionLocal() as s:  # both left open by the restaurant since yesterday
        await s.execute(text("UPDATE orders SET pickup_day = pickup_day - 1"))
        await s.commit()
    r = await place(client, qa, pickup_order(shop_id, [{"product_id": ids[346], "qty": 1}]))
    assert r.status_code == 200, r.text
