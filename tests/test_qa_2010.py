"""QA campaign 20/10/2026 (ODS Delivery, prod), the findings fixed server side: m3, m4, d1-F1, d3-M2,
m5 / m6 (d3), D4-2, D4-6, d6-admin-1, d6-admin-2, d7-M1 (rapport technique qa-2010)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.db import SessionLocal
from app.models import Courier, HotDeal, OrderOffer, User, VitrineRequest
from app.services import vitrine_orders
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, now, pt, reload
from tests.test_no_response import call as emergency
from tests.test_no_response import on_the_way
from tests.test_no_show_1910 import answered_both, time_passes


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture(autouse=True)
def outside_a_prayer(monkeypatch):
    from app.services import prayer_times

    monkeypatch.setattr(prayer_times, "slow_slot", lambda now, where=prayer_times.SOUSSE: None)


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def place_courier(courier, *, at=None, minutes_ago=0.0, seen_minutes_ago=None):
    """His stored position (or none) and its age; the presence heartbeat apart (couriers.last_seen_at)."""
    seen = minutes_ago if seen_minutes_ago is None else seen_minutes_ago
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == courier.id)
            .values(
                last_location=pt(*at) if at else None,
                location_at=now() - timedelta(minutes=minutes_ago) if at else None,
                last_seen_at=now() - timedelta(minutes=seen),
            )
        )
        await s.commit()


# --- m3: a delay the server could not check says so in the customer's notice -------------------------


async def test_m3_unverified_arrival_is_said_in_the_notice(client, world):
    await place_courier(world.courier, at=None, minutes_ago=0)  # position unknown
    order = await world.order()
    r = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 10},
    )
    assert r.status_code == 200, r.text
    [note] = await notifications(world.customer, "new_offer")
    assert note.body_fr.endswith("· chez vous ≈ 10 min (non vérifié)")
    assert note.body_ar.endswith("(غير مؤكَّد)") and note.data["arrival_verified"] is False
    # a price change of that offer: the same mention
    offer_id = r.json()["offer"]["id"]
    edit = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 4.5})
    assert edit.status_code == 200, edit.text
    [edited] = await notifications(world.customer, "new_offer")
    assert "(non vérifié)" in edited.body_fr and edited.data["arrival_verified"] is False


async def test_m3_verified_arrival_has_no_mention(client, world):
    order = await world.order()
    r = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 25},
    )
    assert r.status_code == 200, r.text
    [note] = await notifications(world.customer, "new_offer")
    assert "non vérifié" not in note.body_fr and note.data["arrival_verified"] is True


# --- m4: the presence heartbeat without a position ---------------------------------------------------


async def test_m4_heartbeat_keeps_him_online_without_making_his_position_fresh(client, world):
    await place_courier(world.courier, at=(35.8256, 10.6084), minutes_ago=120)
    r = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"action": "update", "fields": {"heartbeat": True}},
    )
    assert r.status_code == 200, r.text
    courier = await reload(Courier, world.courier.id)
    assert now() - courier.last_seen_at < timedelta(minutes=1)  # still online after 4 h of app open
    assert now() - courier.location_at > timedelta(minutes=100)  # the 2-hour-old position stays old
    # so no « vérifié » arrival is computed from it
    order = await world.order()
    est = (await call(client, world.courier_user, "getOfferEstimate", {"order_id": str(order.id)})).json()
    assert est["success"] is True and est["to_shop"] is None
    # a real fix refreshes both
    r = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"action": "update", "fields": {"current_lat": 35.83, "current_lng": 10.61}},
    )
    courier = await reload(Courier, world.courier.id)
    assert now() - courier.location_at < timedelta(minutes=1)


async def test_m4_heartbeat_of_an_offline_courier_changes_nothing(client, world):
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == world.courier.id)
            .values(is_online=False, last_seen_at=now() - timedelta(hours=5))
        )
        await s.commit()
    await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"action": "update", "fields": {"heartbeat": True}},
    )
    courier = await reload(Courier, world.courier.id)
    assert courier.is_online is False and now() - courier.last_seen_at > timedelta(hours=4)


# --- d1-F1: an old position is never shown as live ---------------------------------------------------


async def test_d1f1_old_position_is_not_live_and_the_accepted_offer_position_is(client, world):
    await place_courier(world.courier, at=(35.80, 10.60), minutes_ago=60 * 28)  # yesterday's fix
    order = await world.order(status="accepted", courier=world.courier, fee="4")
    card = (await call(client, world.customer, "getOrderCourier", {"order_id": str(order.id)})).json()
    courier = card.get("courier", card)
    assert courier["current_lat"] is None and courier["location_updated_at"] is None
    eta = (await call(client, world.customer, "getOrderETA", {"order_id": str(order.id)})).json()
    assert eta["eta_minutes"] is None and eta["reason"] == "courier_location_unavailable"
    # where he was when he sent the offer the customer accepted, 2 minutes ago: that one
    async with SessionLocal() as s:
        s.add(
            OrderOffer(
                order_id=order.id,
                courier_id=world.courier.id,
                proposed_fee=Decimal("4"),
                eta_minutes=15,
                status="accepted",
                courier_lat=Decimal("35.8300"),
                courier_lng=Decimal("10.6200"),
                courier_position_at=now() - timedelta(minutes=2),
            )
        )
        await s.commit()
    card = (await call(client, world.customer, "getOrderCourier", {"order_id": str(order.id)})).json()
    courier = card.get("courier", card)
    assert (courier["current_lat"], courier["current_lng"]) == (35.83, 10.62)
    eta = (await call(client, world.customer, "getOrderETA", {"order_id": str(order.id)})).json()
    assert eta["eta_minutes"] is not None


# --- d3-M2, m5: « pas venu » then resold / kept ------------------------------------------------------


async def no_show(client, world):
    order = await on_the_way(world)
    await answered_both(client, world, order)
    await time_passes(order, 200)
    r = await emergency(client, world.courier_user, "customer_no_show", order)
    assert r.status_code == 200, r.text
    return order, r.json()


async def test_d3m2_m5_no_show_resale_says_he_did_not_come(client, world):
    order, body = await no_show(client, world)
    # m5: two alerts were sent, the « pas venu » record is none (was « reports 3 » of 2, in_app true)
    assert body["reports"] == 2 and body["max_reports"] == 2 and body["channels"]["in_app"] is False
    deal = await call(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert deal.status_code == 200, deal.text
    told = (await notifications(world.customer, "order_cancelled"))[-1]
    assert "pas venu" in told.body_fr and "pas répondu" not in told.body_fr
    assert "لم تأتِ" in told.body_ar and told.data["no_show"] is True


async def test_d3m2_no_show_then_kept_says_he_did_not_come(client, world):
    order, _ = await no_show(client, world)
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
            "reason": "client_no_response",
        },
    )
    assert r.status_code == 200, r.text
    told = (await notifications(world.customer, "order_cancelled"))[-1]
    assert "pas venu" in told.body_fr and "pas répondu" not in told.body_fr


async def test_m5_customer_confirms_answers_the_alert_count(client, world):
    order = await on_the_way(world)
    assert (await emergency(client, world.courier_user, "report_no_response", order)).status_code == 200
    r = await emergency(client, world.customer, "customer_confirms", order)
    assert r.status_code == 200 and r.json()["reports"] == 1  # was null


# --- m6: the buyer of an Offre Chaude cancels: the courier learns it is on sale again ----------------


async def test_m6_courier_told_his_hot_deal_is_on_sale_again(client, world, factory):
    order, _ = await no_show(client, world)
    deal = (await call(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})).json()
    buyer = await factory.user(email="buyer@example.test", full_name="Hela", phone_e164="+21622333444")
    bought = await call(
        client,
        buyer,
        "reserveHotDeal",
        {
            "resale_order_id": deal["deal_id"],
            "delivery_address": "Rue X",
            "delivery_lat": 35.83,
            "delivery_lng": 10.62,
            "phone": "98 765 432",
        },
    )
    assert bought.status_code == 200, bought.text
    resale_order = bought.json()["order_id"]
    r = await call(
        client,
        buyer,
        "cancelOrder",
        {"order_id": resale_order, "cancelled_by": "customer", "reason": "changed_mind"},
    )
    assert r.status_code == 200, r.text
    told = (await notifications(world.courier_user, "order_cancelled"))[-1]
    assert "de nouveau en vente" in told.body_fr and "معروض للبيع من جديد" in told.body_ar
    import uuid

    assert (await reload(HotDeal, uuid.UUID(deal["deal_id"]))).status == "available"


# --- D4-2 / D4-6: a Vitrine line « Sans fromage » + « + Fromage »; the cart refusals say why ---------


def test_d42_removal_and_the_same_paid_extra_clash():
    options = [
        {"key": "cheese", "name": {"fr": "Fromage", "ar": "جبن"}},
        {"key": "onion", "name": {"fr": "Oignon"}},
    ]
    mods = [{"key": "cheese", "level": "none"}, {"key": "onion", "level": "less"}]
    extras = [
        {"key": "x-cheese", "name": {"fr": "fromages", "ar": "fromages"}},
        {"key": "x-egg", "name": "Œuf"},
    ]
    assert vitrine_orders._removed_and_added(mods, options, extras) == ["cheese", "x-cheese"]
    # « Peu de fromage » + « + Fromage »: allowed; another extra: allowed
    assert vitrine_orders._removed_and_added([{"key": "cheese", "level": "less"}], options, extras) == []
    assert vitrine_orders._removed_and_added(mods, options, extras[1:]) == []


def test_d46_invalid_cart_reasons():
    from app.services.orders import OrderRefused

    pid = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(OrderRefused) as bad:
        vitrine_orders._lines([{"product_id": pid, "qty": 1, "mods": [{"key": "a", "level": "huge"}]}])
    assert bad.value.extra.get("reason") == "invalid_line" and "max_lines" not in bad.value.extra
    with pytest.raises(OrderRefused) as twice:
        vitrine_orders._lines([{"product_id": pid, "qty": 1}, {"product_id": pid, "qty": 2}])
    assert twice.value.extra.get("reason") == "duplicate_line"


# --- d6-admin-1 / d6-admin-2: « Masquer les tests » in the requests and the users' figures -----------


async def test_d6_admin1_test_requests_hidden_with_the_box(client, world, factory):
    real = await factory.user(email="resto@gmail.com", full_name="Karim Resto", phone_e164="+21698111222")
    test = await factory.user(email="contact+essai1810@ordering-dashboard-solution.com", full_name="Test4")
    async with SessionLocal() as s:
        for who, name, phone in ((real, "Le Vrai", "+21698111222"), (test, "Essai", "+21698111333")):
            s.add(VitrineRequest(source="app", user_id=who.id, shop_name=name, phone=phone, address="Sousse"))
        await s.commit()
    hidden = (await call(client, world.admin, "listVitrineRequests", {})).json()
    assert [r["shop_name"] for r in hidden["requests"]] == ["Le Vrai"] and hidden["counts"]["pending"] == 1
    every = (await call(client, world.admin, "listVitrineRequests", {"hide_tests": False})).json()
    assert {r["shop_name"] for r in every["requests"]} == {"Le Vrai", "Essai"} and every["counts"][
        "pending"
    ] == 2


async def test_d6_admin2_users_figures_leave_test_orders_out(client, world, factory):
    partner = await factory.user(
        email="coffee@gmail.com", full_name="Coffee and Tea", phone_e164="+21698000111"
    )
    qa_user = await factory.user(email="qa-livreur9@example.test", full_name="QA Livreur", profile=False)
    qa_courier = await world.make_courier(qa_user, display_name="QA Livreur", phone_e164="+21655000999")
    await world.order(partner, items="QA TEST [Vitrine] Burger")
    await world.order(partner, status="delivered", courier=qa_courier, items="Burger")
    real_user = await factory.user(email="sami.livreur@gmail.com", full_name="Sami", profile=False)
    real_courier = await world.make_courier(real_user, display_name="Sami", phone_e164="+21655000888")
    await world.order(
        partner, status="delivered", courier=real_courier, items="Pizza", fee="4", purchase="10"
    )

    async def figures(payload):
        users = (await call(client, world.admin, "listAdminUsers", payload)).json()["users"]
        p = next(u for u in users if u["email"] == "coffee@gmail.com")
        return p["orders"], p["delivered"]

    assert await figures({}) == (1, 1)  # only the real one
    assert await figures({"hide_tests": False}) == (3, 2)


# --- d7-M1: the customer's e-mail never reaches his courier -----------------------------------------


async def test_d7m1_courier_never_reads_the_customer_email(client, world):
    order = await on_the_way(world)
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert doc["customer_id"] is None and doc["created_by"] is None
    assert doc["customer_name"]  # his name to greet him at the door stays
    mine = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert mine["customer_id"] == world.customer.email  # « is it my order? » still works for him

    sent = await call(
        client, world.customer, "sendOrderMessage", {"order_id": str(order.id), "content": "Bonjour"}
    )
    assert sent.status_code == 200, sent.text
    await call(
        client, world.courier_user, "sendOrderMessage", {"order_id": str(order.id), "content": "J'arrive"}
    )
    seen = (await call(client, world.courier_user, "getOrderMessages", {"order_id": str(order.id)})).json()
    blob = str(seen)
    assert world.customer.email not in blob
    by_text = {m["content"]: m for m in seen["messages"]}
    assert by_text["Bonjour"]["sender_id"] == f"u-{world.customer.id}"
    assert by_text["J'arrive"]["sender_id"] == world.courier_user.email  # his own, as is
    unread = (await call(client, world.courier_user, "listMyUnreadMessages", {})).json()
    assert world.customer.email not in str(unread)
    # and the customer never reads the courier's
    theirs = (await call(client, world.customer, "getOrderMessages", {"order_id": str(order.id)})).json()
    assert world.courier_user.email not in str(theirs)

    assert (await emergency(client, world.courier_user, "report_no_response", order)).status_code == 200
    cases = (
        await client.get(
            "/api/entities/NoResponseCase",
            params={"q": f'{{"order_id":"{order.id}"}}'},
            headers=auth(world.courier_user),
        )
    ).json()
    assert cases and all(c["customer_id"] is None for c in cases)
    assert world.customer.email not in str(cases)


async def test_d7m1_user_rows_untouched(world):
    # the e-mails themselves are unchanged (only what each reader is given)
    assert (await reload(User, world.customer.id)).email == "cust@example.test"
