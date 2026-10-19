"""QA campaign 18-19/10/2026, the major findings fixed server side (M3 … M15, rapport technique)."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Courier, Order
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


# --- M4: cancelling during « article indisponible » is a late cancellation -------------------------------


async def stops_at(order, status):
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import OrderStop

    async with SessionLocal() as s:
        await s.execute(update(OrderStop).where(OrderStop.order_id == order.id).values(status=status))
        await s.commit()


async def courier_cancel(client, world, order, reason="vehicle_issue"):
    return await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
            "reason": reason,
        },
    )


async def late_count(world) -> int:
    from tests.order_helpers import reload

    return (await reload(Courier, world.courier.id)).late_cancellations


async def test_m4_price_confirmation_needed_counts_as_late(client, world):
    order = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    await stops_at(order, "at_shop")
    policy = await call(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(order.id), "actor": "courier"}
    )
    assert policy.json()["policy"]["penalty_applies"] is True
    assert policy.json()["policy"]["reason_code"] == "courier_late_cancel_penalty"
    before = await late_count(world)
    r = await courier_cancel(client, world, order)
    assert r.status_code == 200, r.text
    assert await late_count(world) == before + 1


async def test_m4_exceptions_are_kept(client, world):
    before = await late_count(world)
    # « magasin fermé » during the question: no penalty (owner 06/10)
    closed = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    await stops_at(closed, "at_shop")
    assert (await courier_cancel(client, world, closed, "shop_closed")).status_code == 200
    # a Vitrine restaurant's « plat manquant » before he reached the shop: he was not at the shop
    early = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    policy = await call(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(early.id), "actor": "courier"}
    )
    assert policy.json()["policy"]["penalty_applies"] is False
    assert (await courier_cancel(client, world, early)).status_code == 200
    assert await late_count(world) == before


# --- M6: after two alerts the customer answered, the policy never offers « revendez » ------------------


async def answered_twice(order, world):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import NoResponseCase
    from tests.order_helpers import now

    async with SessionLocal() as s:
        for n in range(2):
            start = now() - timedelta(minutes=20 - 5 * n)
            s.add(
                NoResponseCase(
                    order_id=order.id,
                    courier_id=world.courier.id,
                    status="resolved",
                    started_at=start,
                    deadline_at=start + timedelta(minutes=3),
                    resolved_at=start + timedelta(minutes=1),
                    resolution="customer_confirmed",
                )
            )
        await s.commit()


async def test_m6_policy_says_what_he_can_really_do(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="4", purchase="8.4")
    ask = {"order_id": str(order.id), "actor": "courier"}
    # bought, no alert at all: his own choice, the resale is possible (unchanged)
    free = (await call(client, world.courier_user, "getCancellationPolicy", ask)).json()["policy"]
    assert free["can_resell"] is True and "revendez-les" in free["message_fr"]

    await answered_twice(order, world)
    policy = (await call(client, world.courier_user, "getCancellationPolicy", ask)).json()["policy"]
    assert policy["after_purchase"] is True and policy["can_resell"] is False
    assert policy["resale_refusal"] == "customer_answered"
    assert "revend" not in policy["message_fr"].lower()
    assert "terminez la livraison" in policy["message_fr"] and "rendez les articles" in policy["message_fr"]
    # the server agrees: the resale is refused, the return to the shop (+1 late) is the way out
    refused = await call(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "courier_choice": True}
    )
    assert refused.status_code == 409
    back = await courier_cancel(client, world, order, "returned_to_shop")
    assert back.status_code == 200, back.text
