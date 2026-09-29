"""Aurora hot deals: price decay (floor, current / next price, reserve at the current price), the
listing's trust fields, the ResaleOrder decayed price and the opt-in "new hot deal" alerts."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text, update

from app.db import SessionLocal
from app.models import HotDeal, Order, OrderRating, OrderStatusEvent, OrderStop, User
from app.services import hot_deals
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, TUNIS, notifications, pt, reload, rows
from tests.test_hot_deals import a_deal, fn, parked
from tests.test_hot_deals import world as world


async def backdate(deal: HotDeal, minutes: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE hot_deals SET created_at = now() - make_interval(secs => :s) WHERE id = :d"),
            {"s": minutes * 60, "d": deal.id},
        )
        await s.commit()


async def listed(client, user, deal: HotDeal) -> dict:
    body = (await fn(client, user, "listHotDeals", {"id": str(deal.id)})).json()
    [one] = body["deals"]
    return one


def test_price_schedule():
    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    deal = HotDeal(
        start_price=Decimal("10"), floor_price=Decimal("8.2"), drop_step=Decimal("0.5"), drop_every_min=5,
        created_at=t0,
    )  # fmt: skip
    assert hot_deals.price_schedule(deal, t0) == (Decimal("10"), Decimal("9.5"), t0 + timedelta(minutes=5))
    assert hot_deals.current_price(deal, t0 + timedelta(minutes=4, seconds=59)) == Decimal("10")
    at_11 = t0 + timedelta(minutes=11)
    assert hot_deals.price_schedule(deal, at_11) == (
        Decimal("9.0"),
        Decimal("8.5"),
        t0 + timedelta(minutes=15),
    )
    near_floor = t0 + timedelta(minutes=15)
    assert hot_deals.price_schedule(deal, near_floor) == (
        Decimal("8.5"),
        Decimal("8.2"),
        t0 + timedelta(minutes=20),
    )
    assert hot_deals.price_schedule(deal, t0 + timedelta(hours=1)) == (Decimal("8.2"), None, None)
    flat = HotDeal(
        start_price=Decimal("7"),
        floor_price=Decimal("7"),
        drop_step=Decimal("0.5"),
        drop_every_min=5,
        created_at=t0,
    )
    assert hot_deals.price_schedule(flat, t0 + timedelta(hours=1)) == (Decimal("7"), None, None)


async def test_create_deal_floor_default_and_bounds(client, world):
    order = await parked(client, world, purchase="25")
    for bad in (5.9, 20.1, "abc"):  # start = 20 (20 % off 25): floor in [6, 20]
        r = await fn(
            client, world.courier_user, "createHotDeal",
            {"order_id": str(order.id), "discount_percentage": 20, "floor_price": bad},
        )  # fmt: skip
        if bad == "abc":
            assert r.status_code == 400 and r.json()["error"] == "invalid_floor_price"
        else:
            assert r.status_code == 400 and r.json() == {
                "error": "invalid_floor_price",
                "min": 6.0,
                "max": 20.0,
            }
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "discount_percentage": 20}
    )
    assert r.status_code == 200, r.text
    assert r.json()["start_price"] == 20 and r.json()["floor_price"] == 18  # 20 − 4 × 0.5
    deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert (deal.price, deal.start_price, deal.floor_price) == (
        Decimal("20.000"),
        Decimal("20.000"),
        Decimal("18.000"),
    )
    assert (deal.drop_step, deal.drop_every_min) == (Decimal("0.500"), 5)


async def test_small_deal_floor_is_thirty_percent(client, world):
    order = await parked(client, world, purchase="2")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.json()["start_price"] == 2 and r.json()["floor_price"] == 0.6
    order2 = await parked(client, world, purchase="10")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order2.id), "floor_price": 3})
    assert r.status_code == 200 and r.json()["floor_price"] == 3


async def test_listing_shows_the_decay_and_the_trust_fields(client, world):
    source_purchase = "20"
    deal = await a_deal(world, start_price=Decimal("18"), floor_price=Decimal("16"))
    async with SessionLocal() as s:
        s.add(
            OrderStatusEvent(
                order_id=deal.original_order_id,
                from_status="at_shop",
                to_status="purchased",
                source="courier_app",
            )
        )
        await s.execute(
            update(OrderStop)
            .where(OrderStop.order_id == deal.original_order_id)
            .values(receipt_key="private/receipt/x.jpg")
        )
        s.add(OrderRating(order_id=deal.original_order_id, courier_id=world.courier.id, rating=4))
        await s.commit()
    await backdate(deal, 12)  # two drops
    one = await listed(client, world.buyer, deal)
    assert one["original_price"] == one["purchase_amount"] == float(source_purchase)
    assert one["start_price"] == 18 and one["floor_price"] == 16
    assert one["current_price"] == one["discounted_price"] == 17
    assert one["next_price"] == 16.5 and one["next_drop_at"] is not None
    assert one["discount_pct_now"] == 15  # 17 instead of 20
    assert one["courier_name"] == "Karim T." and one["courier_rating"] == 4 and one["courier_deliveries"] == 0
    assert one["receipt_verified"] is True and one["sealed"] is False
    assert one["purchased_at"] is not None and one["listed_at"] == one["created_date"]
    assert one["no_response_at"] is None
    for field in ("courier_phone", "courier_lat", "buyer_id"):
        assert field not in one

    await backdate(deal, 60)
    floor = await listed(client, world.buyer, deal)
    assert floor["current_price"] == 16 and floor["next_price"] is None and floor["next_drop_at"] is None

    # the ResaleOrder entity reads the same decayed price
    doc = (await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.buyer))).json()
    assert doc["discounted_price"] == doc["current_price"] == 16 and doc["start_price"] == 18


async def test_reserve_charges_the_current_price(client, world):
    deal = await a_deal(world, start_price=Decimal("18"), floor_price=Decimal("15"))
    await backdate(deal, 11)
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deal.id), "delivery_address": "Rue 2"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 17
    order = await reload(Order, uuid.UUID(r.json()["order_id"]))
    assert order.purchase_amount == Decimal("17.000") and order.delivery_fee == Decimal("4.000")
    [stop] = await rows(select(OrderStop).where(OrderStop.order_id == order.id))
    assert stop.purchase_amount == Decimal("17.000")
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.buyer))).json()
    assert doc["hot_deal_saving"] == 3  # 20 − 17
    assert doc["free_cancel_until"] is not None


async def test_legacy_deals_do_not_decay(client, world):
    deal = await a_deal(world)  # only the price given: start = floor = price
    assert (deal.start_price, deal.floor_price) == (Decimal("18"), Decimal("18"))
    await backdate(deal, 90)
    one = await listed(client, world.buyer, deal)
    assert one["current_price"] == 18 and one["next_price"] is None


async def opt_in(user: User, at: tuple[float, float] | None, on: bool = True) -> None:
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user.id).values(notify_hot_deals=on))
        await s.commit()
    if at is not None:
        from app.models import UserAddress

        async with SessionLocal() as s:
            s.add(UserAddress(user_id=user.id, is_default=True, address="x", location=pt(*at)))
            await s.commit()


async def test_new_deal_alerts_opted_in_neighbours(client, world, factory):
    near = await factory.user(email="near@example.test")
    await opt_in(near, SOUSSE_HOME)
    await opt_in(world.buyer, TUNIS)  # too far
    quiet = await factory.user(email="quiet@example.test")
    await opt_in(quiet, SOUSSE_HOME, on=False)
    gone = await factory.user(email="gone@example.test", deleted_at=datetime.now(UTC))
    await opt_in(gone, SOUSSE_HOME)
    await opt_in(world.customer, SOUSSE_HOME)  # the original customer: never
    await opt_in(world.courier_user, SOUSSE_SHOP)  # the courier: never
    no_address = await factory.user(email="noaddr@example.test")
    await opt_in(no_address, None)

    order = await parked(client, world, purchase="25")
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "discount_percentage": 20}
    )
    assert r.status_code == 200, r.text
    assert r.json()["alerted"] == 1
    [note] = await notifications(near, "hot_deal_new")
    assert note.title_fr == "🔥 Offre chaude près de chez vous"
    assert note.body_fr == "2x Pain — 20.000 TND au lieu de 25.000 TND"
    assert note.data["resale_order_id"] == r.json()["deal_id"] and note.data["recipient_role"] == "customer"
    for user in (world.buyer, quiet, gone, world.customer, world.courier_user, no_address):
        assert await notifications(user, "hot_deal_new") == []


async def test_no_alert_for_a_qa_deal(client, world, factory):
    near = await factory.user(email="near@example.test")
    await opt_in(near, SOUSSE_HOME)
    order = await parked(client, world, purchase="25", items="QA TEST pain")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 200 and r.json()["alerted"] == 0


async def test_alert_cap(client, world, factory, monkeypatch):
    monkeypatch.setattr(hot_deals, "ALERT_MAX", 2)
    for n in range(3):
        await opt_in(await factory.user(email=f"n{n}@example.test"), SOUSSE_HOME)
    order = await parked(client, world, purchase="25")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.json()["alerted"] == 2


async def test_user_profile_opt_in(client, world):
    url = f"/api/entities/UserProfile/{world.customer.id}"
    assert (await client.get(url, headers=auth(world.customer))).json()["notify_hot_deals"] is False
    r = await client.patch(url, json={"notify_hot_deals": True}, headers=auth(world.customer))
    assert r.status_code == 200 and r.json()["notify_hot_deals"] is True
    assert (await reload(User, world.customer.id)).notify_hot_deals is True
    bad = await client.patch(url, json={"notify_hot_deals": "yes"}, headers=auth(world.customer))
    assert bad.status_code == 400


async def test_the_decay_counts_from_the_listing(client, world):
    deal = await a_deal(world, start_price=Decimal("18"), floor_price=Decimal("16"))
    await backdate(deal, 6)
    assert (await listed(client, world.buyer, deal))["current_price"] == 17.5
