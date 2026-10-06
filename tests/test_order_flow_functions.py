"""placeOrder, dispatchOrderToCouriers, createOrderOffer, acceptOrderOffer, cancelOrder,
getCancellationPolicy: answers, guards, notifications and races."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Courier,
    HotDeal,
    NoResponseCase,
    Order,
    OrderOffer,
    OrderStatusEvent,
    OrderStop,
    User,
)
from app.services import cancellation
from tests.factories import auth
from tests.order_helpers import (
    SOUSSE_HOME,
    SOUSSE_SHOP,
    TUNIS,
    OrderWorld,
    device,
    notifications,
    pushes,
    reload,
    rows,
)


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


def order_form(**overrides):
    form = {
        "items_text": "2x Pain\n1x Lait",
        "quantity": 3,
        "notes": "sans sel",
        "estimated_price": 4.5,
        "package_size": "moyen",
        "shop_name": "Monoprix",
        "shop_address": "Av. Habib Bourguiba",
        "shop_governorate": "Sousse",
        "shop_city": "Sousse",
        "shop_lat": SOUSSE_SHOP[0],
        "shop_lng": SOUSSE_SHOP[1],
        "shops": [
            {
                "name": "Monoprix",
                "address": "Av. Habib Bourguiba",
                "lat": SOUSSE_SHOP[0],
                "lng": SOUSSE_SHOP[1],
            }
        ],
        "delivery_address": "Rue de la plage",
        "delivery_governorate": "Sousse",
        "delivery_city": "Sousse",
        "delivery_details": "3e étage",
        "delivery_lat": SOUSSE_HOME[0],
        "delivery_lng": SOUSSE_HOME[1],
        "preferred_time": "asap",
    }
    form.update(overrides)
    return form


# --- placeOrder ------------------------------------------------------------------------------------


async def test_place_order_builds_the_whitelisted_order_and_dispatches(client, world, factory):
    await device(world.courier_user)
    far_user = await factory.user(email="far@example.test", profile=False)
    await world.make_courier(far_user, at=TUNIS)
    response = await call(
        client,
        world.customer,
        "placeOrder",
        {"order": order_form(courier_id="x", status="delivered", delivery_fee=1, payment_status="paid")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True and body["dispatched"] == 1
    order = body["order"]
    assert order["status"] == "pending" and order["courier_id"] is None and order["delivery_fee"] is None
    assert order["customer_id"] == "cust@example.test" and order["customer_name"] == "Amel Ben Ali"
    assert order["customer_phone"] == "+21622111222" and order["package_size"] == "moyen"
    assert order["shop_name"] == "Monoprix" and len(order["shops"]) == 1 and order["quantity"] == 3
    assert order["distance_km"] == pytest.approx(1.2, abs=0.2) and order["payment_method"] == "cash"
    assert [h["source"] for h in order["status_history"]] == ["placeOrder"]
    assert order["last_dispatched_at"] is not None
    [note] = await notifications(world.courier_user, "new_order")
    assert note.title_fr == "🎯 Nouvelle commande disponible"
    assert note.body_fr == "2x Pain\n1x Lait de Monoprix - À 0,0 km"
    assert note.data["quick_actions"] == ["accept", "decline"] and note.data["recipient_role"] == "courier"
    assert len(await pushes(world.courier_user)) == 1
    assert await notifications(far_user) == []


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"items_text": "  "}, "invalid_items"),
        ({"shop_name": ""}, "invalid_shop"),
        ({"shop_lat": 48.8, "shop_lng": 2.3}, "invalid_shop_location"),
        ({"shop_lat": None}, "invalid_shop_location"),
        ({"delivery_address": ""}, "invalid_delivery_address"),
        ({"delivery_lat": "abc"}, "invalid_delivery_location"),
    ],
)
async def test_place_order_validation(client, world, override, error):
    response = await call(client, world.customer, "placeOrder", {"order": order_form(**override)})
    assert response.status_code == 400 and response.json() == {"error": error}


async def test_place_order_phone_and_defaults(client, world, factory):
    nophone = await factory.user(email="nophone@example.test")
    refused = await call(client, nophone, "placeOrder", {"order": order_form()})
    assert refused.json() == {"error": "phone_required"}
    await factory.address(nophone, address="Maison", location="SRID=4326;POINT(10.62 35.83)")
    body = order_form(
        customer_phone="98 765 432",
        quantity=500,
        package_size="huge",
        estimated_price=-3,
        preferred_time="scheduled",
        scheduled_time="2026-10-01T10:00:00",
    )
    del body["delivery_lat"], body["delivery_lng"]
    placed = await call(client, nophone, "placeOrder", {"order": body})
    assert placed.status_code == 200, placed.text
    order = placed.json()["order"]
    assert order["customer_phone"] == "+21698765432"
    assert order["delivery_lat"] == pytest.approx(35.83)  # the profile's default address
    assert order["quantity"] == 1 and order["package_size"] == "petit" and order["estimated_price"] is None
    assert order["preferred_time"] == "scheduled" and order["scheduled_time"].startswith(
        "2026-10-01T10:00:00"
    )


async def test_a_failed_broadcast_never_fails_the_order(client, world, monkeypatch):
    import app.api.functions.placeOrder as place_module

    async def broken(session, order):
        raise RuntimeError("push provider down")

    monkeypatch.setattr(place_module, "dispatch_order", broken)
    response = await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert response.status_code == 200 and response.json()["dispatched"] is None
    assert len(await rows(select(Order))) == 1


async def test_qa_orders_are_not_broadcast(client, world):
    response = await call(
        client, world.customer, "placeOrder", {"order": order_form(items_text="QA TEST pain")}
    )
    assert response.status_code == 200 and response.json()["dispatched"] is None
    assert await notifications(world.courier_user) == []


async def test_place_order_limits(client, world):
    for _ in range(5):
        await world.order()
    full = await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert full.status_code == 429 and full.json() == {"error": "too_many_open_orders", "max": 5}

    async with SessionLocal() as s:
        order = await s.get(Order, (await world.order(status="cancelled")).id)
        for _ in range(5):
            s.add(
                NoResponseCase(
                    order_id=order.id,
                    status="resolved",
                    started_at=datetime.now(UTC),
                    deadline_at=datetime.now(UTC),
                    incident_counted=True,
                )
            )
        await s.commit()
    suspended = await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert suspended.status_code == 403 and suspended.json() == {"error": "customer_suspended"}
    assert (await client.post("/api/functions/placeOrder", json={})).status_code == 401


async def test_invite_link_courier_is_told_first(client, world, factory):
    referrer_user = await factory.user(email="ref@example.test", profile=False)
    referrer = await world.make_courier(referrer_user, at=TUNIS, service_governorate="Tunis")
    await device(referrer_user)
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        customer = await s.get(User, world.customer.id)
        customer.referred_by_courier_id, customer.referred_at, customer.profile_created_at = (
            referrer.id,
            now,
            now,
        )
        await s.commit()
    response = await call(client, world.customer, "placeOrder", {"order": order_form()})
    order = response.json()["order"]
    assert order["preferred_courier_id"] == str(referrer.id)
    [note] = await notifications(referrer_user, "new_order")
    assert note.title_fr == "⭐ Votre client Amel a passé une commande"
    assert note.data["preferred_courier"] is True and note.body_fr.endswith("km")
    assert response.json()["dispatched"] == 2  # him + the courier at the shop (devices or not, as live)


# --- dispatchOrderToCouriers -----------------------------------------------------------------------


async def test_dispatch_eligibility(client, world, factory):
    async def courier(email, **fields):
        user = await factory.user(email=email, profile=False)
        await world.make_courier(user, **fields)
        return user

    offline = await courier("offline@example.test", is_online=False)
    pending = await courier("pending@example.test", verification="pending")
    far = await courier("far@example.test", at=(35.95, 10.6))  # ~14 km
    wide = await courier("wide@example.test", at=(35.95, 10.6), notification_radius_km=Decimal("20"))
    nopos = await courier("nopos@example.test", at=None)
    busy = await courier("busy@example.test")
    other_gov = await courier("gov@example.test", service_governorate="Monastir")
    busy_courier = (
        await rows(
            select(Courier).join(User, User.id == Courier.user_id).where(User.email == "busy@example.test")
        )
    )[0]
    await world.order(status="at_shop", courier=busy_courier)
    order = await world.order()
    response = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dispatched"] == 2 and body["success"] is True
    notified = {n.user_id for n in await notifications(type_="new_order")}
    assert notified == {world.courier_user.id, wide.id}
    assert not notified & {offline.id, pending.id, far.id, nopos.id, busy.id, other_gov.id}

    again = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    assert again.status_code == 429 and again.json()["error"] == "too_soon"
    assert 0 < again.json()["retry_after_s"] <= 300


async def test_dispatch_governorate_fallback_and_guards(client, world, factory):
    order = await world.order(governorate="Kairouan")
    response = await call(client, world.admin, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    assert response.json()["dispatched"] == 1  # nobody online in Kairouan: every online courier
    stranger = await factory.user(email="s@example.test")
    assert (
        await call(client, stranger, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    ).status_code == 403
    assert (await call(client, world.customer, "dispatchOrderToCouriers", {})).status_code == 400
    missing = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": "nope"})
    assert missing.status_code == 404
    taken = await world.order(status="accepted", courier=world.courier)
    closed = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(taken.id)})
    assert closed.status_code == 409 and closed.json() == {"error": "order_not_open", "status": "accepted"}
    test_order = await world.order(items="PW-12 pain")
    qa = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(test_order.id)})
    assert qa.json() == {"success": True, "dispatched": 0, "reason": "test_order"}
    no_shop = await world.order(shop=None)
    coords = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(no_shop.id)})
    assert coords.json()["reason"] == "shop_coords_missing"


async def test_dispatch_respects_push_preferences(client, world):
    async with SessionLocal() as s:
        user = await s.get(User, world.courier_user.id)
        user.notify_new_orders = False
        await s.commit()
    order = await world.order()
    body = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})).json()
    assert body["dispatched"] == 0 and body["sample_skips"][0]["reason"] == "pref_disabled"
    assert len(await notifications(world.courier_user)) == 1  # the in-app row is always written


async def test_preferred_courier_skip_reasons(client, world, factory):
    referrer_user = await factory.user(email="ref@example.test", profile=False)
    referrer = await world.make_courier(referrer_user, is_online=False)
    order = await world.order(preferred_courier_id=referrer.id)
    body = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})).json()
    assert {"id": str(referrer.id), "reason": "preferred_not_attributed"} in body["sample_skips"]
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        customer = await s.get(User, world.customer.id)
        customer.referred_by_courier_id, customer.referred_at, customer.profile_created_at = (
            referrer.id,
            now,
            now,
        )
        await s.commit()
    await asyncio.sleep(0)
    order2 = await world.order(preferred_courier_id=referrer.id)
    body = (
        await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order2.id)})
    ).json()
    assert {"id": str(referrer.id), "reason": "preferred_offline"} in body["sample_skips"]
    assert body["preferred_notified"] is False


# --- createOrderOffer --------------------------------------------------------------------------------


async def test_create_offer(client, world, factory):
    order = await world.order()
    response = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {
            "order_id": str(order.id),
            "fee": 6.1234,
            "eta_minutes": 25,
            "distance_km": 3.456,
            "message": " Salut ",
            "courier_id": "forged",
            "courier_rating": 5,
            "status": "accepted",
        },
    )
    assert response.status_code == 200, response.text
    offer = response.json()["offer"]
    assert offer["proposed_fee"] == 6.123 and offer["eta_minutes"] == 25 and offer["distance_km"] == 3.46
    assert offer["courier_id"] == str(world.courier.id) and offer["status"] == "pending"
    assert offer["message"] == "Salut" and offer["courier_rating"] == 5
    assert offer["customer_id"] == "cust@example.test"
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["status"] == "offers_received" and doc["status_history"][-1]["source"] == "createOrderOffer"
    # the customer is told by the server (the app's own notice that follows is a duplicate)
    [note] = await notifications(world.customer)
    assert note.type == "new_offer" and note.order_id == order.id
    assert note.body_fr == "Karim T. propose 6.123 DT · ~25 min"
    assert note.data["offer_id"] == offer["id"] and note.data["recipient_role"] == "customer"

    twice = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert twice.status_code == 409 and twice.json() == {"error": "offer_already_sent"}


async def test_create_offer_guards(client, world, factory):
    order = await world.order()
    assert (await call(client, world.courier_user, "createOrderOffer", {"fee": 5})).json() == {
        "error": "Missing order_id"
    }
    for fee in (0, -1, 201, "abc", None):
        r = await call(
            client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": fee}
        )
        assert r.status_code == 400 and r.json() == {"error": "invalid_fee"}, fee
    nobody = await factory.user(email="n@example.test")
    r = await call(client, nobody, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 403 and r.json() == {"error": "courier_profile_missing"}
    pending_user = await factory.user(email="p@example.test", profile=False)
    await world.make_courier(pending_user, verification="pending")
    r = await call(client, pending_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.json() == {"error": "courier_not_verified"}
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": "nope", "fee": 5})
    assert r.status_code == 404 and r.json() == {"error": "order_not_found"}
    dual = await world.order(world.courier_user)
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(dual.id), "fee": 5})
    assert r.status_code == 403 and r.json() == {"error": "own_order"}
    taken = await world.order(status="accepted", courier=world.courier)
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(taken.id), "fee": 5})
    assert r.status_code == 409 and r.json() == {"error": "order_not_open", "status": "accepted"}


async def test_double_offer_race(client, world):
    order = await world.order()
    payload = {"order_id": str(order.id), "fee": 5}
    first, second = await asyncio.gather(
        call(client, world.courier_user, "createOrderOffer", payload),
        call(client, world.courier_user, "createOrderOffer", payload),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert len(await rows(select(OrderOffer))) == 1


# --- acceptOrderOffer ---------------------------------------------------------------------------------


async def test_accept_offer(client, world, factory):
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user)
    order = await world.order(status="offers_received")
    chosen = await world.offer(order, fee="7.5")
    loser = await world.offer(order, other, fee="6")
    response = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(chosen.id)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["courier_user_id"] == "courier@example.test" and body["offer"]["status"] == "accepted"
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["status"] == "accepted" and doc["courier_id"] == str(world.courier.id)
    assert doc["delivery_fee"] == 7.5 and doc["eta_minutes"] == 20 and doc["distance_km"] == 1.5
    assert doc["accepted_at"] is not None and doc["status_history"][-1]["source"] == "acceptOrderOffer"
    assert (await reload(OrderOffer, loser.id)).status == "rejected"
    # the courier is told by the server, always pushed
    [note] = await notifications(world.courier_user)
    assert note.type == "order_accepted"
    assert note.body_fr == "Offre acceptée — Monoprix : 2x Pain. Allez au magasin."
    again = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(loser.id)}
    )
    assert again.status_code == 409 and again.json() == {"error": "order_not_open", "status": "accepted"}


async def test_accept_offer_guards(client, world, factory):
    order = await world.order(status="offers_received")
    offer = await world.offer(order)
    payload = {"order_id": str(order.id), "offer_id": str(offer.id)}
    assert (
        await call(client, world.customer, "acceptOrderOffer", {"order_id": str(order.id)})
    ).status_code == 400
    stranger = await factory.user(email="s@example.test")
    assert (await call(client, stranger, "acceptOrderOffer", payload)).json() == {"error": "Forbidden"}
    missing = await call(client, world.customer, "acceptOrderOffer", {"order_id": "x", "offer_id": "y"})
    assert missing.status_code == 404 and missing.json() == {"error": "order_not_found"}
    wrong = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": "bad"}
    )
    assert wrong.json() == {"error": "offer_not_found"}
    other_order = await world.order(status="offers_received")
    foreign = await world.offer(other_order)
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(foreign.id)}
    )
    assert r.status_code == 404
    expired = await world.offer(order, status="expired", fee="3")
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(expired.id)}
    )
    assert r.status_code == 409 and r.json() == {"error": "offer_not_pending"}
    async with SessionLocal() as s:
        courier = await s.get(Courier, world.courier.id)
        courier.verification = "rejected"
        await s.commit()
    r = await call(client, world.customer, "acceptOrderOffer", payload)
    assert r.status_code == 409 and r.json() == {"error": "courier_unavailable"}


async def test_suspended_customer_cannot_accept_but_admin_can(client, world):
    order = await world.order(status="offers_received")
    offer = await world.offer(order)
    async with SessionLocal() as s:
        old = await world.order(status="cancelled")
        for _ in range(5):
            s.add(
                NoResponseCase(
                    order_id=old.id,
                    status="resolved",
                    started_at=datetime.now(UTC),
                    deadline_at=datetime.now(UTC),
                    incident_counted=True,
                )
            )
        await s.commit()
    payload = {"order_id": str(order.id), "offer_id": str(offer.id)}
    r = await call(client, world.customer, "acceptOrderOffer", payload)
    assert r.status_code == 403 and r.json() == {"error": "customer_suspended", "incidents": 5}
    assert (await call(client, world.admin, "acceptOrderOffer", payload)).status_code == 200


async def test_two_accepts_at_once_one_wins(client, world, factory):
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user)
    order = await world.order(status="offers_received")
    a, b = await world.offer(order), await world.offer(order, other, fee="6")
    results = await asyncio.gather(
        call(client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(a.id)}),
        call(client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(b.id)}),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    statuses = sorted(o.status for o in await rows(select(OrderOffer)))
    assert statuses == ["accepted", "rejected"]
    events = await rows(select(OrderStatusEvent).where(OrderStatusEvent.to_status == "accepted"))
    assert len(events) == 1  # one acceptance only


# --- cancelOrder ------------------------------------------------------------------------------------


async def test_customer_cancels(client, world):
    await device(world.courier_user)
    async with SessionLocal() as s:
        user = await s.get(User, world.courier_user.id)
        user.notify_order_status = False  # cancellations are pushed anyway
        await s.commit()
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    pending_offer_order = await world.order(status="offers_received")
    pending = await world.offer(pending_offer_order)
    r = await call(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": str(order.id), "reason": "changed_mind", "cancelled_by": "customer"},
    )
    assert r.status_code == 200 and r.json() == {"success": True, "message": "Order cancelled successfully"}
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert doc["status"] == "cancelled" and doc["cancelled_by"] == "customer"
    assert doc["cancellation_reason"] == "changed_mind" and doc["cancelled_at"] is not None
    assert doc["status_history"][-1] == {
        **doc["status_history"][-1],
        "cancelled_by": "customer",
        "reason": "changed_mind",
        "source": "cancelOrder",
    }
    [note] = await notifications(world.courier_user, "order_cancelled")
    assert note.body_fr == "Le client a annulé la commande (Monoprix) : changement d'avis"
    assert note.data == {"reason": "changed_mind", "cancelled_by": "customer", "recipient_role": "courier"}
    assert len(await pushes(world.courier_user)) == 1

    await call(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": str(pending_offer_order.id), "reason": "x", "cancelled_by": "customer"},
    )
    assert (await reload(OrderOffer, pending.id)).status == "rejected"
    late = await world.order(status="at_shop", courier=world.courier)
    r = await call(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": str(late.id), "reason": "x", "cancelled_by": "customer"},
    )
    assert r.status_code == 400 and r.json() == {
        "error": "Cannot cancel order at this stage",
        "can_cancel": False,
    }


async def test_cancel_guards(client, world, factory):
    order = await world.order(status="accepted", courier=world.courier)
    base = {"order_id": str(order.id), "reason": "x"}
    assert (await call(client, world.customer, "cancelOrder", {"order_id": str(order.id)})).status_code == 400
    r = await call(client, world.customer, "cancelOrder", {**base, "cancelled_by": "qa"})
    assert r.status_code == 400 and r.json() == {"error": "Invalid cancelled_by"}
    missing = await call(
        client, world.customer, "cancelOrder", {**base, "order_id": "nope", "cancelled_by": "customer"}
    )
    assert missing.status_code == 404
    stranger = await factory.user(email="s@example.test")
    assert (
        await call(client, stranger, "cancelOrder", {**base, "cancelled_by": "customer"})
    ).status_code == 403
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user)
    r = await call(
        client, other_user, "cancelOrder", {**base, "cancelled_by": "courier", "courier_id": str(other.id)}
    )
    assert r.status_code == 403
    r = await call(
        client,
        other_user,
        "cancelOrder",
        {**base, "cancelled_by": "courier", "courier_id": str(world.courier.id)},
    )
    assert r.status_code == 403
    done = await world.order(status="delivered", courier=world.courier, fee="5")
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(done.id),
            "reason": "x",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 400 and r.json()["can_cancel"] is False


async def test_courier_drops_back_to_the_pool(client, world, factory):
    other_user = await factory.user(email="c2@example.test", profile=False)
    await world.make_courier(other_user)
    await device(world.customer)
    # before the purchase (at the shop, one shop already paid): back to the pool
    order = await world.order(status="at_shop", courier=world.courier, fee="5", purchase="12", stops=2)
    offer = await world.offer(order, status="accepted")
    async with SessionLocal() as s:
        for stop in (await s.execute(select(OrderStop).where(OrderStop.order_id == order.id))).scalars():
            stop.status, stop.purchase_amount = "purchased", Decimal("6")
        await s.commit()
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "vehicle_issue",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["status"] == "pending" and doc["courier_id"] is None and doc["delivery_fee"] is None
    assert doc["cancelled_by"] == "courier" and doc["purchase_amount"] is None
    assert {s["status"] for s in doc["shops"]} == {"pending"} and doc["current_shop_index"] == 0
    assert (
        doc["status_history"][-1]["status"] == "pending"
        and doc["status_history"][-1]["cancelled_by"] == "courier"
    )
    assert (await reload(OrderOffer, offer.id)).status == "expired"
    assert (await reload(Courier, world.courier.id)).late_cancellations == 1
    [note] = await notifications(world.customer, "order_cancelled")
    assert note.title_fr == "⚠️ Le livreur a annulé"
    assert (
        note.body_fr == "Raison : problème de véhicule. Votre commande est de nouveau proposée aux livreurs."
    )
    # broadcast again to the couriers around the shop
    notified = {n.user_id for n in await notifications(type_="new_order")}
    assert other_user.id in notified
    # ...but never to the courier who gave it up, and he can no longer bid on it (owner, 2026-09-29)
    assert world.courier_user.id not in notified
    again = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert again.status_code == 409 and again.json()["error"] == "order_dropped"
    # ...nor read or list it, so it never comes back as « Meilleure course » on another device (B21)
    assert (
        await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))
    ).status_code == 404
    listed = await client.get(
        "/api/entities/Order", params={"q": '{"status": "pending"}'}, headers=auth(world.courier_user)
    )
    assert listed.status_code == 200 and str(order.id) not in {o["id"] for o in listed.json()}
    # the other couriers still see it, to bid
    assert (await client.get(f"/api/entities/Order/{order.id}", headers=auth(other_user))).status_code == 200


async def test_courier_early_drop_has_no_penalty(client, world):
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "emergency",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert (await reload(Courier, world.courier.id)).late_cancellations == 0


async def test_verified_no_response_closes_the_order(client, world):
    order = await world.order(status="client_no_response", courier=world.courier, fee="5", purchase="20")
    past = datetime.now(UTC) - timedelta(minutes=20)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                courier_id=world.courier.id,
                status="expired",
                started_at=past,
                deadline_at=past + timedelta(minutes=10),
            )
        )
        await s.commit()
    refreshed = []

    async def refresh(session, locked):
        refreshed.append(locked.id)

    installed = cancellation.no_response_refresh
    cancellation.no_response_refresh = refresh
    try:
        r = await call(
            client,
            world.courier_user,
            "cancelOrder",
            {
                "order_id": str(order.id),
                "reason": "goods_returned_to_shop",
                "cancelled_by": "courier",
                "courier_id": str(world.courier.id),
            },
        )
    finally:
        cancellation.no_response_refresh = installed
    assert r.status_code == 200 and refreshed == [order.id]
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["status"] == "cancelled" and doc["no_response_resolution"] == "returned_to_shop"
    [case] = await rows(select(NoResponseCase))
    assert case.status == "resolved" and case.incident_counted is True and case.final_at is not None
    assert (await reload(Courier, world.courier.id)).late_cancellations == 0
    [note] = await notifications(world.customer, "order_cancelled")
    assert "incident de non-réponse" in note.body_fr
    assert await notifications(type_="new_order") == []  # not re-dispatched


async def test_unverified_no_response_never_sends_the_bought_goods_to_another_courier(client, world):
    """Owner's rule (06/10, QA B26, wave 3): during the countdown the goods are paid, so the order is
    never put back for other couriers; the courier can only return them to the shop (or resell
    after the deadline)."""
    order = await world.order(status="client_no_response", courier=world.courier, fee="5", purchase="20")
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                courier_id=world.courier.id,
                status="waiting",
                started_at=datetime.now(UTC),
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await s.commit()
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "client_no_response",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 409 and r.json()["error"] == "after_purchase_resell_or_return"
    assert (await reload(Order, order.id)).status == "client_no_response"
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "returned_to_shop",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 200, r.text
    [case] = await rows(select(NoResponseCase))
    assert case.resolution == "courier_cancelled_other" and case.incident_counted is False
    assert (await reload(Order, order.id)).status == "cancelled"
    assert (await reload(Courier, world.courier.id)).late_cancellations == 1
    assert await notifications(type_="new_order") == []  # not re-dispatched


def test_no_response_gate():
    now = datetime.now(UTC)
    case = NoResponseCase(
        status="waiting", started_at=now - timedelta(minutes=5), deadline_at=now + timedelta(minutes=1)
    )
    assert cancellation.no_response_gate("client_no_response", None, now) == {
        "ok": False,
        "reason": "no_report",
    }
    assert cancellation.no_response_gate("client_no_response", case, now)["reason"] == "wait"
    case.deadline_at = None
    assert cancellation.no_response_gate("client_no_response", case, now) == {"ok": True}  # legacy 2 min wait
    case.status, case.resolution = "resolved", "courier_reached"
    assert cancellation.no_response_gate("client_no_response", case, now)["reason"] == "customer_answered"
    case.resolution = "auto_closed"
    assert cancellation.no_response_gate("client_no_response", case, now)["reason"] == "already_closed"
    case.resolution = "customer_confirmed"
    assert cancellation.no_response_gate("on_the_way", case, now)["reason"] == "customer_answered"


async def test_hot_deal_cancellations(client, world, factory):
    buyer = await factory.user(email="buyer@example.test", phone_e164="+21622000111")
    source = await world.order(status="cancelled", courier=world.courier, fee="5")

    async def deal(expires_in: timedelta) -> HotDeal:
        async with SessionLocal() as s:
            row = HotDeal(
                original_order_id=source.id,
                courier_id=world.courier.id,
                items_text="x",
                shop_name="S",
                purchase_amount=Decimal("10"),
                discount_percentage=Decimal("20"),
                price=Decimal("8"),
                expires_at=datetime.now(UTC) + expires_in,
                status="sold",
                buyer_id=buyer.id,
                reserved_at=datetime.now(UTC),
            )
            s.add(row)
            await s.commit()
            return row

    live_deal = await deal(timedelta(hours=1))
    bought = await world.order(
        buyer, status="accepted", courier=world.courier, fee="3", resale_deal_id=live_deal.id
    )
    r = await call(
        client, buyer, "cancelOrder", {"order_id": str(bought.id), "reason": "x", "cancelled_by": "customer"}
    )
    assert r.status_code == 200
    relisted = await reload(HotDeal, live_deal.id)
    assert relisted.status == "available" and relisted.buyer_id is None

    old_deal = await deal(timedelta(hours=-1))
    bought2 = await world.order(
        buyer, status="accepted", courier=world.courier, fee="3", resale_deal_id=old_deal.id
    )
    await call(
        client, buyer, "cancelOrder", {"order_id": str(bought2.id), "reason": "x", "cancelled_by": "customer"}
    )
    assert (await reload(HotDeal, old_deal.id)).status == "expired"

    third = await deal(timedelta(hours=1))
    bought3 = await world.order(
        buyer, status="on_the_way", courier=world.courier, fee="3", resale_deal_id=third.id
    )
    r = await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(bought3.id),
            "reason": "returned_to_shop",  # the goods are bought: return or resell only (B26)
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 200
    assert (await reload(Order, bought3.id)).status == "cancelled"  # not back to the pool
    assert (await reload(HotDeal, third.id)).status == "expired"
    [note] = await notifications(buyer, "order_cancelled")
    assert note.title_fr == "⚠️ Le livreur ne peut pas terminer la livraison"


# --- getCancellationPolicy -------------------------------------------------------------------------


async def test_cancellation_policy(client, world, factory):
    order = await world.order(status="at_shop", courier=world.courier)
    r = await call(client, world.customer, "getCancellationPolicy", {"order_id": str(order.id)})
    body = r.json()
    assert body["actor"] == "customer" and body["status"] == "at_shop"
    # owner's rule (06/10, B25): no customer cancel once the courier is at the shop
    assert body["policy"]["reason_code"] == "courier_at_shop" and body["policy"]["can_cancel"] is False
    r = await call(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(order.id), "actor": "courier"}
    )
    assert r.json()["policy"]["reason_code"] == "courier_late_cancel_penalty"
    early = await world.order()
    policy = (
        await call(client, world.customer, "getCancellationPolicy", {"order_id": str(early.id)})
    ).json()["policy"]
    assert policy["reason_code"] == "free_cancel" and policy["message_fr"] == "Vous pouvez annuler sans frais"
    done = await world.order(status="on_the_way", courier=world.courier)
    policy = (await call(client, world.customer, "getCancellationPolicy", {"order_id": str(done.id)})).json()[
        "policy"
    ]
    assert policy["can_cancel"] is False
    policy = (
        await call(
            client,
            world.courier_user,
            "getCancellationPolicy",
            {"order_id": str(early.id), "actor": "courier"},
        )
    ).json()
    assert policy["error"] == "Forbidden"  # not his order (yet)
    stranger = await factory.user(email="s@example.test")
    assert (
        await call(client, stranger, "getCancellationPolicy", {"order_id": str(order.id)})
    ).status_code == 403
    assert (
        await call(client, world.admin, "getCancellationPolicy", {"order_id": str(order.id)})
    ).status_code == 200
    assert (await call(client, world.customer, "getCancellationPolicy", {})).status_code == 400
    assert (await call(client, world.customer, "getCancellationPolicy", {"order_id": "x"})).status_code == 404


async def test_place_order_foreign_phone_while_whatsapp_is_off(client, world, factory, monkeypatch):
    from app.config import settings

    abroad = await factory.user(email="abroad@example.test", phone_e164="+33612345678")
    accepted = await call(client, abroad, "placeOrder", {"order": order_form()})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["order"]["customer_phone"] == "+33612345678"

    # Once WhatsApp works, a foreign number must be verified first (the verification flow).
    monkeypatch.setattr(type(settings), "whatsapp_enabled", property(lambda self: True))
    other = await factory.user(email="abroad2@example.test", phone_e164="+41791234567")
    refused = await call(client, other, "placeOrder", {"order": order_form()})
    assert refused.json() == {"error": "phone_unverified"}


async def test_after_the_purchase_the_courier_resells_or_returns_never_drops(client, world):
    """Owner's rule (06/10, QA B26): the goods are paid — no plain cancel, never back to the pool."""
    order = await world.order(status="purchased", courier=world.courier, fee="5", purchase="12")
    body = {"order_id": str(order.id), "cancelled_by": "courier", "courier_id": str(world.courier.id)}
    r = await call(client, world.courier_user, "cancelOrder", {**body, "reason": "vehicle_issue"})
    assert r.status_code == 409 and r.json()["error"] == "after_purchase_resell_or_return"
    r = await call(client, world.courier_user, "cancelOrder", {**body, "reason": "returned_to_shop"})
    assert r.status_code == 200, r.text
    fresh = await reload(Order, order.id)
    assert fresh.status == "cancelled" and fresh.cancel_reason == "returned_to_shop"
    [note] = await notifications(world.customer, "order_cancelled")
    assert (
        note.title_fr == "⚠️ Le livreur ne peut pas terminer la livraison" and note.data["fault_free"] is True
    )


async def test_shop_closed_no_penalty_and_not_sent_to_others(client, world):
    """Owner's rule (06/10, QA B31)."""
    order = await world.order(status="at_shop", courier=world.courier, fee="5")
    r = await call(
        client, world.courier_user, "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "shop_closed",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).status == "cancelled"
    assert (await reload(Courier, world.courier.id)).late_cancellations == 0
    [note] = await notifications(world.customer, "order_cancelled")
    assert note.title_fr == "🏪 Magasin fermé" and "autre magasin" in note.body_fr
