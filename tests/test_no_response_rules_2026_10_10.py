"""Owner's rules of 10/10/2026 on customers who don't answer at the door: the couriers' badge
« n'a pas payé X fois sur ses Y dernières commandes » (instead of a suspension), and the ODS
commission on a resold order only when it was sold at full price."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import SessionLocal
from app.models import NoResponseCase, Order
from app.services import commission
from app.services import orders as order_rules
from tests.factories import auth
from tests.order_helpers import OrderWorld


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, body=None):
    return await client.post("/api/functions/getCustomerReliability", json=body or {}, headers=auth(user))


async def unpaid(world, created_at: datetime, counted: bool = True) -> Order:
    order = await world.order(status="cancelled", courier=world.courier, created_at=created_at)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id, status="resolved", started_at=created_at, deadline_at=created_at,
                final_at=created_at, incident_counted=counted,
            )
        )  # fmt: skip
        await s.commit()
    return order


# ─────────────────────────── the couriers' badge ───────────────────────────


async def test_badge_counts_the_last_finished_orders(client, world):
    base = datetime.now(UTC) - timedelta(days=30)
    for i in range(3):
        await world.order(status="delivered", courier=world.courier, created_at=base + timedelta(hours=i))
    await unpaid(world, base + timedelta(hours=5))
    await unpaid(world, base + timedelta(hours=6), counted=False)  # answered late: not unpaid
    await world.order(status="cancelled", created_at=base + timedelta(hours=7))  # never bought: ignored
    own = (await call(client, world.customer)).json()
    assert own["recent_orders"] == 4 and own["recent_unpaid"] == 1
    open_order = await world.order()
    seen = (await call(client, world.courier_user, {"order_id": str(open_order.id)})).json()
    assert seen["visible_to_couriers"] is True and seen["recent_unpaid"] == 1


async def test_badge_window_is_the_last_ten(client, world):
    old = datetime.now(UTC) - timedelta(days=400)  # out of the 180-day incident window
    await unpaid(world, old)
    recent = datetime.now(UTC) - timedelta(days=5)
    for i in range(order_rules.RECENT_ORDERS - 1):
        await world.order(status="delivered", courier=world.courier, created_at=recent + timedelta(minutes=i))
    own = (await call(client, world.customer)).json()
    # the old unpaid order is still one of his last 10: shown, although its incident expired
    assert own["incidents"] == 0 and own["recent_orders"] == 10 and own["recent_unpaid"] == 1
    open_order = await world.order()
    seen = (await call(client, world.courier_user, {"order_id": str(open_order.id)})).json()
    assert seen["visible_to_couriers"] is True
    await world.order(status="delivered", courier=world.courier, created_at=recent + timedelta(hours=1))
    own = (await call(client, world.customer)).json()
    assert own["recent_orders"] == 10 and own["recent_unpaid"] == 0


async def test_clean_customer_has_no_badge(client, world):
    await world.order(status="delivered", courier=world.courier)
    open_order = await world.order()
    seen = (await call(client, world.courier_user, {"order_id": str(open_order.id)})).json()
    assert seen["visible_to_couriers"] is False and seen["recent_unpaid"] == 0


# ─────────────────────────── commission on resales ───────────────────────────


async def resale_order(world, *, deal_purchase: str, paid: str) -> Order:
    from tests.test_hot_deals import a_deal

    deal = await a_deal(world, purchase_amount=Decimal(deal_purchase), price=Decimal(paid))
    return await world.order(
        world.factory_buyer, status="delivered", courier=world.courier, fee="4", purchase=paid,
        resale_deal_id=deal.id,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("deal_purchase", "paid", "charged"),
    [("20", "20", True), ("20", "18", False), ("20", "19.500", False)],
)
async def test_resale_commission_only_at_full_price(world, factory, deal_purchase, paid, charged):
    world.factory = factory
    world.factory_buyer = await factory.user(email="buyer@example.test")
    order = await resale_order(world, deal_purchase=deal_purchase, paid=paid)
    async with SessionLocal() as s:
        entry = await commission.record_delivery(s, await s.get(Order, order.id), datetime.now(UTC))
        await s.commit()
    assert (entry is not None) is charged


async def test_regular_order_still_carries_the_commission(world):
    order = await world.order(status="delivered", courier=world.courier, fee="4", purchase="20")
    async with SessionLocal() as s:
        entry = await commission.record_delivery(s, await s.get(Order, order.id), datetime.now(UTC))
        await s.commit()
    assert entry is not None and entry.amount == commission.COMMISSION_PER_DELIVERY_TND
