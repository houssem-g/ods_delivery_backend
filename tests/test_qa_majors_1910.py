"""QA campaign 18-19/10/2026, the major findings fixed server side (M3 … M15, rapport technique)."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Order
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, OrderWorld, device, notifications, rows


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


def order_form(**overrides):
    form = {
        "items_text": "2x Pain\n1x Lait",
        "shop_name": "Monoprix",
        "shop_address": "Av. Habib Bourguiba",
        "shop_governorate": "Sousse",
        "shop_lat": SOUSSE_SHOP[0],
        "shop_lng": SOUSSE_SHOP[1],
        "delivery_address": "Rue de la plage",
        "delivery_governorate": "Sousse",
        "delivery_lat": SOUSSE_HOME[0],
        "delivery_lng": SOUSSE_HOME[1],
        "preferred_time": "asap",
    }
    form.update(overrides)
    return form


async def count_orders(customer) -> int:
    return len(await rows(select(Order.id).where(Order.customer_id == customer.id)))


# --- M3: a delivered order sent again after a lost answer is never a duplicate -------------------------


async def test_m3_same_client_ref_answers_the_same_delivery_order(client, world, factory):
    await device(world.courier_user)
    ref = uuid.uuid4().hex
    first = await call(client, world.customer, "placeOrder", {"order": order_form(client_ref=ref)})
    assert first.status_code == 200, first.text
    assert first.json()["dispatched"] == 1 and "existing" not in first.json()
    # the answer was lost: the app sends the same attempt again
    again = await call(client, world.customer, "placeOrder", {"order": order_form(client_ref=ref)})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["order"]["id"] == first.json()["order"]["id"]
    assert body["existing"] is True and body["dispatched"] == 0
    assert await count_orders(world.customer) == 1
    assert len(await notifications(world.courier_user, "new_order")) == 1  # never broadcast twice

    # a new attempt (new key) is a new order; no key at all: every call is a new order (older apps)
    other = await call(
        client, world.customer, "placeOrder", {"order": order_form(client_ref=uuid.uuid4().hex)}
    )
    assert other.json()["order"]["id"] != first.json()["order"]["id"]
    await call(client, world.customer, "placeOrder", {"order": order_form()})
    await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert await count_orders(world.customer) == 4

    # the same key from another customer never reaches the first one's order
    someone = await factory.user(email="other@example.test", full_name="Sonia", phone_e164="+21622999000")
    theirs = await call(client, someone, "placeOrder", {"order": order_form(client_ref=ref)})
    assert theirs.status_code == 200 and theirs.json()["order"]["id"] != first.json()["order"]["id"]


async def test_m3_vitrine_delivery_retry_is_idempotent(client, world):
    from tests.test_vitrine_orders import public_vitrine
    from tests.vitrine_helpers import cart_order

    shop_id, [cafe, _] = await public_vitrine(client, world.admin)
    ref = "cart-" + uuid.uuid4().hex[:12]
    body = cart_order(shop_id, [{"product_id": cafe, "qty": 2}], vitrine={"client_ref": ref})
    first = await call(client, world.customer, "placeOrder", body)
    assert first.status_code == 200, first.text
    again = await call(client, world.customer, "placeOrder", body)
    assert again.status_code == 200 and again.json()["existing"] is True
    assert again.json()["order"]["id"] == first.json()["order"]["id"]
    assert await count_orders(world.customer) == 1
