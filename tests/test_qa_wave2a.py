"""QA campaign 06/10 — wave 2, « Passer une commande et suivre » + customer cancellations.

B7 the « en route » notice gives the ride's real time (as the tracking ring), not the offer delay ·
B29 a courier with a pending offer is told when the customer cancels."""

from typing import Any

import httpx

from app.models import OrderOffer
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, reload, set_live


async def fn(client, user, name: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/api/functions/{name}", json=body, headers=auth(user))


# ─────────────────────────── B29 ───────────────────────────


async def test_customer_cancel_tells_the_couriers_who_had_an_offer(client, factory):
    world = await OrderWorld(factory).setup()
    other_user = await factory.user(email="other@example.test", full_name="Sami", profile=False)
    other = await world.make_courier(other_user, display_name="Sami")
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="6")
    theirs = await world.offer(order, other, fee="7")
    r = await fn(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": str(order.id), "cancelled_by": "customer", "reason": "changed_mind"},
    )
    assert r.status_code == 200, r.text
    assert (await reload(OrderOffer, mine.id)).status == "rejected"
    for user in (world.courier_user, other_user):
        notes = await notifications(user, "order_cancelled")
        assert len(notes) == 1
        assert "offre n'est plus valable" in notes[0].body_fr
        assert notes[0].data["offer_closed"] is True
    assert (await reload(OrderOffer, theirs.id)).status == "rejected"
    # the customer himself gets nothing about it
    assert await notifications(world.customer, "order_cancelled") == []


# ─────────────────────────── B7 ───────────────────────────


async def test_on_the_way_notice_uses_the_ride_time_not_the_offer_delay(client, factory):
    world = await OrderWorld(factory).setup()
    order = await world.order(status="purchased", courier=world.courier, fee="5", purchase="10", eta_minutes=53)
    r = await client.patch(
        f"/api/entities/Order/{order.id}", json={"status": "on_the_way"}, headers=auth(world.courier_user)
    )
    assert r.status_code == 200, r.text
    (note,) = await notifications(world.customer, "on_the_way")
    assert "53" not in note.body_fr
    eta = await fn(client, world.customer, "getOrderETA", {"order_id": str(order.id)})
    minutes = eta.json()["eta_minutes"]
    assert minutes and f"~{minutes} min" in note.body_fr


async def test_hot_deal_eta_goes_to_the_customer_from_the_acceptance(client, factory):
    from app.models import HotDeal

    world = await OrderWorld(factory).setup()
    source = await world.order(status="cancelled", courier=world.courier, purchase="20")
    from decimal import Decimal

    from app.db import SessionLocal
    from tests.order_helpers import SOUSSE_SHOP

    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id, courier_id=world.courier.id, items_text="2x Pain",
            purchase_amount=Decimal("20"), discount_percentage=Decimal("10"), price=Decimal("18"),
            delivery_fee=Decimal("4"), pickup_location=f"SRID=4326;POINT({SOUSSE_SHOP[1]} {SOUSSE_SHOP[0]})",
            expires_at=source.created_at.replace(year=source.created_at.year + 1),
        )  # fmt: skip
        s.add(deal)
        await s.commit()
        await s.refresh(deal)
    order = await world.order(status="accepted", courier=world.courier, resale_deal_id=deal.id)
    r = await fn(client, world.customer, "getOrderETA", {"order_id": str(order.id)})
    plain = await world.order(status="accepted", courier=world.courier)
    r2 = await fn(client, world.customer, "getOrderETA", {"order_id": str(plain.id)})
    # the courier stands at the shop: the plain order is there already, the hot deal rides home
    assert r.json()["distance_km"] > r2.json()["distance_km"]
