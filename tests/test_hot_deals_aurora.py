"""Aurora hot deals: the fixed price (owner 19/10/2026: no automatic drop, no floor — the old decay only
reads the history and is frozen for listed deals), the listing's trust fields, the ResaleOrder price
and the opt-in "new hot deal" alerts."""

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


def test_an_old_decaying_deal_keeps_its_history_price():
    """current_price still reads the columns of a deal decayed before 19/10/2026 (its history)."""
    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    deal = HotDeal(
        start_price=Decimal("10"), floor_price=Decimal("8.2"), drop_step=Decimal("0.5"), drop_every_min=5,
        created_at=t0,
    )  # fmt: skip
    assert hot_deals.current_price(deal, t0 + timedelta(minutes=11)) == Decimal("9.0")
    fixed = HotDeal(
        start_price=Decimal("10"), floor_price=Decimal("10"), drop_step=Decimal("0"), created_at=t0
    )
    assert hot_deals.current_price(fixed, t0 + timedelta(hours=2)) == Decimal("10")


async def test_create_deal_at_a_fixed_price(client, world):
    order = await parked(client, world, purchase="25")
    r = await fn(
        client,
        world.courier_user,
        "createHotDeal",
        {"order_id": str(order.id), "discount_percentage": 20, "floor_price": 6},  # an older app's floor
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] == body["start_price"] == 20 and body["price_fixed"] is True
    for gone in ("floor_price", "drop_step", "drop_every_min", "floor_after_min", "price_schedule"):
        assert body[gone] is None, gone
    deal = await reload(HotDeal, uuid.UUID(body["deal_id"]))
    assert (deal.price, deal.start_price, deal.floor_price, deal.drop_step) == (
        Decimal("20.000"),
        Decimal("20.000"),
        Decimal("20.000"),
        Decimal("0"),
    )


async def test_discount_up_to_ninety_percent(client, world):
    """R47, confirmed by the owner 19/10/2026: up to 90 % (never capped at 50 on the server)."""
    order = await parked(client, world, purchase="10")
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "discount_percentage": 90}
    )
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 1  # 10 − 90 %, fixed
    deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert deal.discount_percentage == Decimal("90") and deal.floor_price == deal.price == Decimal("1.000")
    assert hot_deals.MAX_DISCOUNT == 90


async def test_listing_shows_the_fixed_price_and_the_trust_fields(client, world):
    source_purchase = "20"
    deal = await a_deal(world)  # 18, drop_step 0 (the column's default since 19/10/2026)
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
    await backdate(deal, 12)  # formerly two drops: the price does not move any more
    one = await listed(client, world.buyer, deal)
    assert one["original_price"] == one["purchase_amount"] == float(source_purchase)
    assert one["start_price"] == 18 and one["floor_price"] is None
    assert one["current_price"] == one["discounted_price"] == 18
    assert one["next_price"] is None and one["next_drop_at"] is None
    assert one["discount_pct_now"] == 10  # 18 instead of 20
    assert one["courier_name"] == "Karim T." and one["courier_rating"] == 4 and one["courier_deliveries"] == 0
    assert one["receipt_verified"] is True and one["sealed"] is False
    assert one["purchased_at"] is not None and one["listed_at"] == one["created_date"]
    assert one["no_response_at"] is None
    for field in ("courier_phone", "courier_lat", "buyer_id"):
        assert field not in one

    await backdate(deal, 110)
    later = await listed(client, world.buyer, deal)
    assert later["current_price"] == 18 and later["next_price"] is None and later["next_drop_at"] is None

    # the ResaleOrder entity reads the same fixed price, no floor
    doc = (await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.buyer))).json()
    assert doc["discounted_price"] == doc["current_price"] == 18 and doc["start_price"] == 18
    assert doc["floor_price"] is None


async def test_reserve_charges_the_fixed_price(client, world):
    deal = await a_deal(world, start_price=Decimal("17"), floor_price=Decimal("17"), price=Decimal("17"))
    await backdate(deal, 11)  # formerly two drops: still 17
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
    assert note.title_fr == "🔥 Offre Chaude près de chez vous"
    assert note.body_fr == "2x Pain — 20.000 DT au lieu de 25.000 DT"
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


async def test_listed_decaying_deals_are_frozen_never_raised(world):
    """A deal published with a drop (older server) keeps the price it has when the job runs, for good."""
    deal = await a_deal(world, start_price=Decimal("18"), floor_price=Decimal("16"), drop_step=Decimal("0.5"))
    sold_before = await a_deal(
        world,
        start_price=Decimal("18"),
        floor_price=Decimal("16"),
        drop_step=Decimal("0.5"),
        status="sold",
        buyer_id=world.customer.id,
    )
    await backdate(deal, 11)  # two drops: 17
    async with SessionLocal() as s:
        await hot_deals.expire_deals(s)
        await s.commit()
    frozen = await reload(HotDeal, deal.id)
    assert (frozen.price, frozen.start_price, frozen.floor_price, frozen.drop_step) == (
        Decimal("17.000"),
        Decimal("17.000"),
        Decimal("17.000"),
        Decimal("0.000"),
    )
    await backdate(deal, 60)
    assert hot_deals.current_price(await reload(HotDeal, deal.id), datetime.now(UTC)) == Decimal("17.000")
    assert (await reload(HotDeal, sold_before.id)).drop_step == Decimal("0.500")  # history untouched
