"""Aurora order additions: the courier's basket (picked_items), budget_max, the "same courier"
reorder, courier_is_online, the hot-deal saving and free-cancel window, and the stock check's
missing price and quantity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update

from app.db import SessionLocal
from app.models import Courier, HotDeal, Order
from tests.factories import auth
from tests.order_helpers import OrderWorld, reload
from tests.test_order_flow_functions import order_form


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.rival_user = await factory.user(email="rival@example.test", profile=False)
    w.rival = await w.make_courier(w.rival_user, display_name="Sami")
    return w


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def doc(client, user, order) -> dict[str, Any]:
    r = await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


async def write(client, world, order, body, user=None):
    return await client.patch(
        f"/api/entities/Order/{order.id}", json=body, headers=auth(user or world.courier_user)
    )


# ─────────────────────────── picked_items ───────────────────────────


async def test_the_courier_ticks_his_basket(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5", items="Pain\nLait\nCoca")
    r = await write(client, world, order, {"picked_items": [2, 0]})
    assert r.status_code == 200, r.text
    assert r.json()["picked_items"] == [2, 0] and r.json()["status"] == "at_shop"
    assert (await doc(client, world.customer, order))["picked_items"] == [2, 0]
    assert (await doc(client, world.admin, order))["picked_items"] == [2, 0]
    for bad in ([1, 1], [True], [200], [-1], ["1"], list(range(201)), "0,1", {"a": 1}):
        r = await write(client, world, order, {"picked_items": bad})
        assert r.status_code == 400 and r.json()["error"] == "validation_error", bad
    # the whole order spread into a step write: unchanged picked_items is fine
    r = await write(
        client, world, order, {"status": "purchased", "purchase_amount": 12, "picked_items": [2, 0]}
    )
    assert r.status_code == 200, r.text
    r = await write(client, world, order, {"picked_items": [0, 1, 2]})
    assert r.status_code == 403  # no longer shopping
    assert (await reload(Order, order.id)).picked_items == [2, 0]
    # the customer can't write it
    r = await write(client, world, order, {"picked_items": [1]}, user=world.customer)
    assert r.status_code == 403


async def test_picked_items_while_accepted_and_during_a_stock_check(client, world):
    order = await world.order(status="accepted", courier=world.courier, fee="5", items="Pain\nLait")
    assert (await write(client, world, order, {"picked_items": [1]})).status_code == 200
    r = await call(
        client,
        world.courier_user,
        "reportUnavailableItems",
        {"order_id": str(order.id), "missing_text": "Lait"},
    )
    assert r.status_code == 200, r.text
    ticked = await write(client, world, order, {"picked_items": [0, 1]})
    assert ticked.status_code == 200 and ticked.json()["status"] == "price_confirmation_needed"
    step = await write(client, world, order, {"status": "at_shop"})
    assert step.status_code == 409 and step.json()["error"] == "stock_check_pending"
    cleared = await write(client, world, order, {"picked_items": None})
    assert cleared.status_code == 200 and cleared.json()["picked_items"] is None


# ─────────────────────────── placeOrder: budget, same courier ───────────────────────────


async def test_budget_max_is_shown_to_the_bidders(client, world):
    r = await call(client, world.customer, "placeOrder", {"order": order_form(budget_max="25.5")})
    assert r.status_code == 200, r.text
    order_id = r.json()["order"]["id"]
    assert r.json()["order"]["budget_max"] == 25.5
    bidder = await client.get(f"/api/entities/Order/{order_id}", headers=auth(world.rival_user))
    assert bidder.json()["budget_max"] == 25.5
    for bad in (-1, 2001, "abc"):
        r = await call(client, world.customer, "placeOrder", {"order": order_form(budget_max=bad)})
        assert r.status_code == 400 and r.json() == {"error": "invalid_budget", "max": 2000.0}, bad
    none = await call(client, world.customer, "placeOrder", {"order": order_form(budget_max=None)})
    assert none.json()["order"]["budget_max"] is None


async def test_same_courier_only_if_he_delivered_before(client, world, factory):
    await world.order(status="delivered", courier=world.courier, fee="5")
    r = await call(
        client,
        world.customer,
        "placeOrder",
        {"order": order_form(preferred_courier_id=str(world.courier.id))},
    )
    assert r.status_code == 200, r.text
    assert r.json()["order"]["preferred_courier_id"] == str(world.courier.id)

    # never delivered to him (only cancelled): ignored, the referral attribution stays
    await world.order(status="cancelled", courier=world.rival, fee="5")
    referrer_user = await factory.user(email="ref@example.test", profile=False)
    referrer = await world.make_courier(referrer_user, display_name="Ref")
    async with SessionLocal() as s:
        from app.models import User

        now = datetime.now(UTC)
        await s.execute(
            update(User)
            .where(User.id == world.customer.id)
            .values(referred_by_courier_id=referrer.id, referred_at=now, profile_created_at=now)
        )
        await s.commit()
    for given in (str(world.rival.id), "not-a-uuid", None):
        r = await call(
            client, world.customer, "placeOrder", {"order": order_form(preferred_courier_id=given)}
        )
        assert r.status_code == 200, r.text
        assert r.json()["order"]["preferred_courier_id"] == str(referrer.id), given
    # a courier who delivered another customer's order is not "his" courier
    other = await factory.user(email="other@example.test", phone_e164="+21622333444")
    r = await call(
        client, other, "placeOrder", {"order": order_form(preferred_courier_id=str(world.courier.id))}
    )
    assert r.json()["order"]["preferred_courier_id"] is None


# ─────────────────────────── computed fields ───────────────────────────


async def test_courier_is_online_for_the_customer(client, world):
    order = await world.order(status="delivered", courier=world.courier, fee="5")
    assert (await doc(client, world.customer, order))["courier_is_online"] is True
    assert (await doc(client, world.courier_user, order))["courier_is_online"] is None
    async with SessionLocal() as s:
        await s.execute(update(Courier).where(Courier.id == world.courier.id).values(is_online=False))
        await s.commit()
    assert (await doc(client, world.customer, order))["courier_is_online"] is False
    waiting = await world.order()
    assert (await doc(client, world.customer, waiting))["courier_is_online"] is False


async def test_hot_deal_saving_and_free_cancel_window(client, world, factory):
    buyer = await factory.user(email="buyer@example.test")
    source = await world.order(status="cancelled", courier=world.courier, purchase="30")
    reserved = datetime.now(UTC) - timedelta(minutes=1)
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id, courier_id=world.courier.id, items_text="Pain", shop_name="M",
            purchase_amount=Decimal("30"), discount_percentage=Decimal("20"), price=Decimal("24"),
            expires_at=datetime.now(UTC) + timedelta(hours=1), status="sold", buyer_id=buyer.id,
            reserved_at=reserved,
        )  # fmt: skip
        s.add(deal)
        await s.commit()
    bought = await world.order(
        buyer, status="accepted", courier=world.courier, fee="3", purchase="22.5", resale_deal_id=deal.id
    )
    async with SessionLocal() as s:
        await s.execute(update(HotDeal).where(HotDeal.id == deal.id).values(buyer_order_id=bought.id))
        await s.commit()
    body = await doc(client, buyer, bought)
    assert body["hot_deal_saving"] == 7.5
    until = datetime.fromisoformat(body["free_cancel_until"]).replace(tzinfo=UTC)
    assert abs((until - (reserved + timedelta(minutes=3))).total_seconds()) < 0.01
    assert (await doc(client, world.courier_user, bought))["hot_deal_saving"] is None
    plain = await world.order(status="pending")
    body = await doc(client, world.customer, plain)
    assert body["hot_deal_saving"] is None and body["free_cancel_until"] is None


# ─────────────────────────── stock checks ───────────────────────────


async def test_stock_check_missing_price_and_quantity(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5", items="Coca 1L x2")
    payload = {"order_id": str(order.id), "missing_text": "Coca 1L"}
    for extra, error in (
        ({"missing_price": -1}, "invalid_missing_price"),
        ({"missing_price": 2001}, "invalid_missing_price"),
        ({"quantity": 0}, "invalid_quantity"),
        ({"quantity": 101}, "invalid_quantity"),
        ({"quantity": 1.5}, "invalid_quantity"),
    ):
        r = await call(client, world.courier_user, "reportUnavailableItems", {**payload, **extra})
        assert r.status_code == 400 and r.json()["error"] == error, extra
    r = await call(
        client,
        world.courier_user,
        "reportUnavailableItems",
        {**payload, "missing_price": 2.35, "quantity": 2},
    )
    assert r.status_code == 200, r.text
    check = r.json()["stock_check"]
    assert check["missing_price"] == 2.35 and check["quantity"] == 2
    body = await doc(client, world.customer, order)
    assert body["stock_check"]["missing_price"] == 2.35 and body["stock_check"]["quantity"] == 2
    assert body["stock_checks"][0]["quantity"] == 2


async def test_stock_check_quantity_defaults_to_one(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5")
    r = await call(
        client,
        world.courier_user,
        "reportUnavailableItems",
        {"order_id": str(order.id), "missing_text": "Pain"},
    )
    assert r.json()["stock_check"]["quantity"] == 1 and r.json()["stock_check"]["missing_price"] is None
