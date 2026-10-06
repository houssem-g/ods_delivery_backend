"""The courier's delivery steps (Order.update from CourierOrderActive), the transition service
and the commission ledger written at delivery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    CourierLedgerEntry,
    CourierStatement,
    File,
    Order,
    OrderStatusEvent,
    OrderStop,
    OrderTracking,
)
from app.services import commission
from app.services import order_transitions as ot
from tests.factories import auth, error_of
from tests.order_helpers import OrderWorld, device, notifications, pushes, reload, rows, set_live

BASE = "http://localhost:9110/ods-delivery-test"


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def step(client, world, order, body, user=None):
    return await client.patch(
        f"/api/entities/Order/{order.id}", json=body, headers=auth(user or world.courier_user)
    )


async def receipt(world, name="r.jpg") -> str:
    key = f"public/receipt/2026/09/{name}"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key,
                owner_id=world.courier_user.id,
                visibility="public",
                content_type="image/jpeg",
                size_bytes=3,
            )
        )
        await s.commit()
    return f"{BASE}/{key}"


async def test_single_shop_delivery_end_to_end(client, world):
    await device(world.customer)
    order = await world.order(status="accepted", courier=world.courier, fee="7", eta_minutes=12)
    await set_live(order, world.courier, 35.82, 10.61)

    at_shop = await step(
        client,
        world,
        order,
        {
            "status": "at_shop",
            "status_history": [{"status": "at_shop", "lat": 35.8256, "lng": 10.6084}],
        },
    )
    assert at_shop.status_code == 200, at_shop.text
    doc = at_shop.json()
    assert doc["status"] == "at_shop" and doc["shops"][0]["status"] == "at_shop"
    assert doc["status_history"][-1]["lat"] == pytest.approx(35.8256)
    assert doc["status_history"][-1]["source"] == "courier_app"

    url = await receipt(world)
    bought = await step(
        client,
        world,
        order,
        {
            "status": "purchased",
            "purchase_amount": 23.456,
            "receipt_photo_url": url,
            "total_amount": 1,
            "status_history": [{"status": "forged"}] * 9,
        },
    )
    assert bought.status_code == 200, bought.text
    doc = bought.json()
    assert doc["purchase_amount"] == 23.456 and doc["total_amount"] == pytest.approx(30.456)
    assert doc["receipt_photo_url"] == url and doc["shops"][0]["status"] == "purchased"
    assert len(doc["status_history"]) == 4  # pending, accepted (test data), at_shop, purchased

    assert (await step(client, world, order, {"status": "on_the_way"})).status_code == 200
    delivered = await step(
        client,
        world,
        order,
        {
            "status": "delivered",
            "platform_fee": 99,
            "courier_net_earning": 0,
            "ods_commission": 9,
            "ods_commission_status": "due",
        },
    )
    assert delivered.status_code == 200
    doc = delivered.json()
    assert doc["status"] == "delivered" and doc["delivered_at"] is not None
    assert doc["platform_fee"] == 0 and doc["courier_net_earning"] == 7
    assert (
        doc["ods_commission"] == float(commission.COMMISSION_PER_DELIVERY_TND)
        and doc["ods_commission_status"] == "offered_launch"
    )
    assert doc["courier_live_lat"] is None  # the live position left the order
    assert await rows(select(OrderTracking)) == []

    # the server told the customer of each step (the app's own notices are duplicates)
    assert [n.type for n in await notifications(world.customer)] == [
        "at_shop", "purchased", "on_the_way", "delivered",
    ]  # fmt: skip
    assert len(await pushes(world.customer)) == 4

    # delivered is final for the courier
    assert (await step(client, world, order, {"status": "on_the_way"})).status_code == 403


async def test_multi_shop_steps(client, world):
    order = await world.order(status="accepted", courier=world.courier, fee="5", stops=3)
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    shops = doc["shops"]

    def with_stop(index, **changes):
        updated = [dict(s) for s in shops]
        updated[index].update(changes)
        return updated

    shops = with_stop(0, status="at_shop")
    r = await step(client, world, order, {"status": "at_shop", "shops": shops, "current_shop_index": 7})
    assert r.status_code == 200 and r.json()["current_shop_index"] == 0
    url = await receipt(world, "a.jpg")
    shops = with_stop(0, status="purchased", purchase_amount=10, receipt_photo_url=url)
    r = await step(client, world, order, {"status": "at_shop", "shops": shops, "purchase_amount": 999})
    assert r.status_code == 200
    doc = r.json()
    assert doc["current_shop_index"] == 1 and doc["purchase_amount"] == 10 and doc["status"] == "at_shop"

    # a stop can't go back, a name can't be changed, a purchase needs an amount
    back = with_stop(0, status="at_shop")
    assert (await step(client, world, order, {"status": "at_shop", "shops": back})).status_code == 403
    renamed = with_stop(1, name="Elsewhere")
    same = await step(client, world, order, {"status": "at_shop", "shops": renamed})
    assert same.status_code == 200 and same.json()["shops"][1]["name"] == "Shop 2"
    no_amount = with_stop(1, status="purchased")
    assert (await step(client, world, order, {"status": "at_shop", "shops": no_amount})).status_code == 400
    too_much = with_stop(1, status="purchased", purchase_amount=2500)
    assert (await step(client, world, order, {"status": "at_shop", "shops": too_much})).status_code == 400
    assert (await step(client, world, order, {"status": "at_shop", "shops": shops[:2]})).status_code == 400
    # 'purchased' while a shop is still to do
    early = await step(
        client,
        world,
        order,
        {"status": "purchased", "shops": with_stop(1, status="purchased", purchase_amount=3)},
    )
    assert early.status_code == 400

    shops = with_stop(1, status="purchased", purchase_amount=3)
    assert (await step(client, world, order, {"status": "at_shop", "shops": shops})).status_code == 200
    shops = with_stop(2, status="purchased", purchase_amount=2.5)
    done = await step(client, world, order, {"status": "purchased", "shops": shops, "total_amount": 1})
    assert done.status_code == 200
    doc = done.json()
    assert doc["status"] == "purchased" and doc["purchase_amount"] == 15.5 and doc["total_amount"] == 20.5
    assert doc["current_shop_index"] == 2
    assert all(s["completed_at"].endswith("Z") for s in doc["shops"])
    # shops only change while shopping
    assert (
        await step(client, world, order, {"status": "purchased", "shops": shops})
    ).status_code == 200  # no-op
    moved = with_stop(0, status="pending")
    assert (await step(client, world, order, {"status": "on_the_way", "shops": moved})).status_code == 403


async def test_step_guards(client, world, factory):
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    skip = await step(client, world, order, {"status": "delivered"})
    assert skip.status_code == 403 and error_of(skip) == "permission_denied"
    assert (await step(client, world, order, {"status": "on_the_way"})).status_code == 403  # not a hot deal
    assert (await step(client, world, order, {"status": "cancelled"})).status_code == 403  # cancelOrder only
    assert (await step(client, world, order, {"status": "nonsense"})).status_code == 400
    fee = await step(client, world, order, {"status": "at_shop", "delivery_fee": 1})
    assert fee.status_code == 403
    assert (await reload(Order, order.id)).status == "accepted"  # nothing written
    await step(client, world, order, {"status": "at_shop"})
    for amount in (0, -5, 2001, "abc", None):
        r = await step(client, world, order, {"status": "purchased", "purchase_amount": amount})
        assert r.status_code == 400, amount
    foreign = await step(
        client,
        world,
        order,
        {"status": "purchased", "purchase_amount": 5, "receipt_photo_url": "https://evil.example/x.jpg"},
    )
    assert foreign.status_code == 400
    other_user = await factory.user(email="c2@example.test", profile=False)
    await world.make_courier(other_user)
    theirs = f"{BASE}/public/receipt/2026/09/other.jpg"
    async with SessionLocal() as s:
        s.add(
            File(
                key="public/receipt/2026/09/other.jpg",
                owner_id=other_user.id,
                visibility="public",
                content_type="image/jpeg",
                size_bytes=3,
            )
        )
        await s.commit()
    stolen = await step(
        client, world, order, {"status": "purchased", "purchase_amount": 5, "receipt_photo_url": theirs}
    )
    assert stolen.status_code == 400
    ok = await step(
        client, world, order, {"status": "purchased", "purchase_amount": 5, "receipt_photo_url": None}
    )
    assert ok.status_code == 200
    # a repeated tap is a no-op
    again = await step(client, world, order, {"status": "purchased"})
    assert again.status_code == 200
    assert len(await rows(select(OrderStatusEvent).where(OrderStatusEvent.order_id == order.id))) == 4


async def test_no_response_fallback_is_refused(client, world):
    """NoResponsePanel's fallback write (OWN_BACKEND_CLIENT §7 row 20)."""
    order = await world.order(status="client_no_response", courier=world.courier, fee="5")
    r = await step(
        client,
        world,
        order,
        {
            "status": "on_the_way",
            "customer_responded_to_emergency": True,
            "status_history": [],
        },
    )
    assert r.status_code == 403
    plain = await step(client, world, order, {"status": "on_the_way"})
    assert plain.status_code == 403  # the no-response procedure moves this order, not the app


async def test_hot_deal_order_goes_straight_on_the_way(client, world):
    from app.models import HotDeal

    source = await world.order(status="cancelled", courier=world.courier, fee="5")
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id,
            courier_id=world.courier.id,
            items_text="x",
            purchase_amount=Decimal("10"),
            discount_percentage=Decimal("20"),
            price=Decimal("8"),
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            status="sold",
            buyer_id=world.customer.id,
        )
        s.add(deal)
        await s.commit()
    order = await world.order(
        status="accepted", courier=world.courier, fee="3", purchase="8", resale_deal_id=deal.id
    )
    r = await step(client, world, order, {"status": "on_the_way"})
    assert r.status_code == 200 and r.json()["resale_order_id"] == str(deal.id)
    assert r.json()["total_amount"] == 11


# --- transition service -------------------------------------------------------------------------


def test_matrix():
    assert ot.can_transition("pending", "offers_received")
    assert ot.can_transition(None, "pending") and ot.can_transition(None, "accepted")
    assert not ot.can_transition("delivered", "cancelled")
    assert not ot.can_transition("cancelled", "pending")
    assert not ot.can_transition("pending", "delivered")
    assert ot.can_transition("on_the_way", "client_no_response")
    assert ot.can_transition("client_no_response", "on_the_way")
    for status in ot.LIVE_STATUSES:
        assert ot.can_transition(status, "pending") and ot.can_transition(status, "cancelled")
    assert set(ot.ALLOWED) == {
        None,
        *ot.ALLOWED["pending"],
        "pending",
        *ot.LIVE_STATUSES,
        "delivered",
        "cancelled",
    }


async def test_transition_service(world):
    order = await world.order(status="accepted", courier=world.courier)
    await set_live(order, world.courier, 35.8, 10.6)
    async with SessionLocal() as s:
        locked = await ot.lock_order(s, order.id)
        with pytest.raises(ot.InvalidTransition):
            await ot.transition(s, locked, "delivered", world.courier_user.id, "test")
        event = await ot.transition(s, locked, "cancelled", None, "test", "why", cancelled_by="system")
        await s.commit()
    assert event.from_status == "accepted" and event.cancelled_by == "system" and event.actor_user_id is None
    row = await reload(Order, order.id)
    assert row.status == "cancelled" and row.cancelled_at is not None
    assert await rows(select(OrderTracking)) == []
    async with SessionLocal() as s:
        assert await ot.lock_order(s, "not-a-uuid") is None
        fresh = await world.order()
        pending = await ot.lock_order(s, fresh.id)
        pending.courier_id = None
        with pytest.raises(ot.InvalidTransition):
            await ot.transition(s, pending, "accepted", None, "test")  # no courier
        with pytest.raises(ot.InvalidTransition):
            await ot.start(s, pending, None, "test", status="delivered")


# --- commission ledger ----------------------------------------------------------------------------

TUNIS = commission.TUNIS


async def deliver_at(world, when: datetime, fee: str = "5") -> Order:
    order = await world.order(status="on_the_way", courier=world.courier, fee=fee)
    async with SessionLocal() as s:
        row = await s.get(Order, order.id)
        row.status, row.delivered_at = "delivered", when
        await s.flush()
        await commission.record_delivery(s, row, when)
        await s.commit()
    return order


async def kinds(world) -> list[str]:
    entries = await rows(select(CourierLedgerEntry).order_by(CourierLedgerEntry.id))
    return [e.kind for e in entries]


async def test_launch_deliveries_are_waived(world):
    await deliver_at(world, datetime(2026, 12, 31, 22, 59, tzinfo=UTC))  # 23:59 in Tunis
    await deliver_at(world, datetime(2026, 12, 31, 23, 0, tzinfo=UTC))  # midnight in Tunis: paid era
    assert await kinds(world) == ["commission_waived_launch", "commission_waived_quota"]


async def test_monthly_quota_then_due(world):
    march = datetime(2027, 3, 1, 8, tzinfo=TUNIS)
    for i in range(21):
        await deliver_at(world, march + timedelta(hours=i))
    got = await kinds(world)
    assert got.count("commission_waived_quota") == 20 and got[-1] == "commission_due"
    await deliver_at(world, datetime(2027, 4, 1, 0, 30, tzinfo=TUNIS))  # a new month in Tunis
    assert (await kinds(world))[-1] == "commission_waived_quota"
    assert all(
        e.amount == commission.COMMISSION_PER_DELIVERY_TND for e in await rows(select(CourierLedgerEntry))
    )


async def test_no_fee_no_commission_and_idempotent(world):
    free = await deliver_at(world, datetime(2027, 5, 3, tzinfo=TUNIS), fee="0.000")
    assert await kinds(world) == []
    paid = await deliver_at(world, datetime(2027, 5, 3, tzinfo=TUNIS))
    async with SessionLocal() as s:
        row = await s.get(Order, paid.id)
        again = await commission.record_delivery(s, row, row.delivered_at)
        await s.commit()
    assert again is not None and len(await kinds(world)) == 1
    assert free.id != paid.id


async def test_weekly_statements(world, factory):
    async def due(created_at: datetime) -> None:
        order = await world.order(status="delivered", courier=world.courier, fee="5")
        async with SessionLocal() as s:
            s.add(
                CourierLedgerEntry(
                    courier_id=world.courier.id,
                    order_id=order.id,
                    kind="commission_due",
                    amount=Decimal("0.5"),
                    created_at=created_at,
                )
            )
            await s.commit()

    monday = datetime(2027, 3, 8, 4, tzinfo=TUNIS)  # run time
    await due(datetime(2027, 3, 1, 9, tzinfo=TUNIS))
    await due(datetime(2027, 3, 7, 23, 30, tzinfo=TUNIS))  # Sunday night, still last week
    await due(datetime(2027, 2, 24, 9, tzinfo=TUNIS))  # a week the job missed
    await due(datetime(2027, 3, 8, 1, tzinfo=TUNIS))  # this week: next run
    async with SessionLocal() as s:
        result = await commission.build_statements(s, monday)
        await s.commit()
    assert result == {"statements_created": 2, "entries_grouped": 3}
    statements = await rows(select(CourierStatement).order_by(CourierStatement.period_start))
    assert [(str(st.period_start), str(st.period_end), st.total_due) for st in statements] == [
        ("2027-02-22", "2027-02-28", Decimal("0.500")),
        ("2027-03-01", "2027-03-07", Decimal("1.000")),
    ]
    assert all(st.status == "open" for st in statements)
    async with SessionLocal() as s:
        assert await commission.build_statements(s, monday) == {"statements_created": 0, "entries_grouped": 0}
        # an entry of a closed week written late joins its statement
        await s.commit()
    await due(datetime(2027, 3, 2, 9, tzinfo=TUNIS))
    async with SessionLocal() as s:
        late = await commission.build_statements(s, monday + timedelta(days=1))
        await s.commit()
    assert late == {"statements_created": 0, "entries_grouped": 1}
    march = (await rows(select(CourierStatement).order_by(CourierStatement.period_start)))[1]
    assert march.total_due == Decimal("1.500")
    unassigned = await rows(select(CourierLedgerEntry).where(CourierLedgerEntry.statement_id.is_(None)))
    assert len(unassigned) == 1  # this week's


async def test_statements_job_runs(client, world):
    response = await client.post("/api/admin/jobs/courier_statements/run", headers=auth(world.admin))
    assert response.json()["ok"] is True
    assert response.json()["result"] == {"statements_created": 0, "entries_grouped": 0}


async def test_private_receipt_is_stored_and_never_exposed(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5")
    mine = "private/receipt/2026/09/mine.jpg"
    theirs = "private/receipt/2026/09/theirs.jpg"
    async with SessionLocal() as s:
        for key, owner in ((mine, world.courier_user.id), (theirs, world.customer.id)):
            s.add(
                File(key=key, owner_id=owner, visibility="private", content_type="image/jpeg", size_bytes=3)
            )
        await s.commit()

    refused = await step(
        client, world, order, {"status": "purchased", "purchase_amount": 5, "receipt_photo_url": theirs}
    )
    assert refused.status_code == 400

    bought = await step(
        client, world, order, {"status": "purchased", "purchase_amount": 5, "receipt_photo_url": mine}
    )
    assert bought.status_code == 200, bought.text
    async with SessionLocal() as s:
        stop = (await s.execute(select(OrderStop).where(OrderStop.order_id == order.id))).scalar_one()
    assert stop.receipt_key == mine
    for user in (world.customer, world.courier_user):
        doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))).json()
        assert doc["receipt_photo_url"] is None and doc["shops"][0].get("receipt_photo_url") is None
        assert mine not in str(doc)
