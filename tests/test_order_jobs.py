"""expire_stale_orders, orphan offers,
courier presence expiry, and the job registrations."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.jobs.registry import JOBS
from app.models import Courier, NoResponseCase, Order, OrderOffer, OrderTracking
from app.services import expiry
from app.services import order_transitions as ot
from tests.factories import auth
from tests.order_helpers import (
    OrderWorld,
    age,
    age_order,
    device,
    notifications,
    pushes,
    reload,
    rows,
    set_live,
)


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def run_expiry() -> dict:
    async with SessionLocal() as s:
        result = await expiry.expire_stale_orders(s)
        await s.commit()
    return result


async def test_open_orders_expire_after_24h(world):
    await device(world.customer)
    fresh = await world.order()
    old_pending = await world.order()
    old_offers = await world.order(status="offers_received")
    pending_offer = await world.offer(old_offers)
    rejected = await world.offer(old_offers, status="rejected", fee="4")
    await age_order(old_pending, 25)
    await age_order(old_offers, 30)
    await age_order(fresh, 23)

    result = await run_expiry()
    assert sorted(c["id"] for c in result["closed"]) == sorted([str(old_pending.id), str(old_offers.id)])
    assert (await reload(Order, fresh.id)).status == "pending"
    p = await reload(Order, old_pending.id)
    assert (p.status, p.cancelled_by, p.cancel_reason) == ("cancelled", "system", "expired_no_offer")
    assert (await reload(Order, old_offers.id)).cancel_reason == "expired"
    assert (await reload(OrderOffer, pending_offer.id)).status == "expired"
    assert (await reload(OrderOffer, rejected.id)).status == "rejected"
    notes = await notifications(world.customer)
    assert len(notes) == 2 and {n.type for n in notes} == {"order_cancelled"}
    assert all(n.data["recipient_role"] == "customer" and n.data["auto_expired"] for n in notes)
    assert any("Aucun livreur n'a pris votre commande (Monoprix) en 24 h" in n.body_fr for n in notes)
    assert len(await pushes(world.customer)) == 2
    history = (await rows(select(Order).where(Order.id == old_pending.id)))[0]
    assert history.cancelled_at is not None

    again = await run_expiry()
    assert again["closed"] == [] and len(await notifications(world.customer)) == 2


async def test_live_position_does_not_keep_an_abandoned_delivery_alive(client, world):
    ride = await world.order(status="on_the_way", courier=world.courier, fee="5")
    recent = await world.order(status="at_shop", courier=world.courier, fee="5")
    await age_order(ride, 49)
    await set_live(ride, world.courier, 35.8, 10.6)  # written a minute ago
    await age_order(recent, 47)

    result = await run_expiry()
    assert [c["id"] for c in result["closed"]] == [str(ride.id)]
    r = await reload(Order, ride.id)
    assert (r.status, r.cancelled_by, r.cancel_reason, r.courier_id) == (
        "cancelled",
        "system",
        "abandoned",
        None,
    )
    assert await rows(select(OrderTracking)) == []
    assert (await reload(Order, recent.id)).status == "at_shop"
    to = sorted((n.user_id, n.data["recipient_role"]) for n in await notifications(type_="order_cancelled"))
    assert to == sorted([(world.customer.id, "customer"), (world.courier_user.id, "courier")])
    courier_note = (await notifications(world.courier_user))[0]
    assert (
        courier_note.title_fr == "Livraison clôturée" and "sans pénalité ni incident" in courier_note.body_fr
    )
    assert (await reload(Courier, world.courier.id)).late_cancellations == 0
    doc = (await client.get(f"/api/entities/Order/{ride.id}", headers=auth(world.customer))).json()
    assert doc["status_history"][-1]["source"] == "expireStaleOrders"


async def test_a_locked_order_is_left_to_its_writer(world):
    """A customer accepting an offer at that second keeps his order (the row is skipped, not waited on)."""
    racy = await world.order(status="offers_received")
    await age_order(racy, 30)
    async with SessionLocal() as customer_tx:
        locked = await ot.lock_order(customer_tx, racy.id)
        locked.courier_id = world.courier.id
        await ot.transition(customer_tx, locked, "accepted", world.customer.id, "acceptOrderOffer")
        result = await asyncio.wait_for(run_expiry(), timeout=10)
        await customer_tx.commit()
    assert result["closed"] == []
    assert (await reload(Order, racy.id)).status == "accepted"


async def test_bounded_per_run_oldest_first(world):
    orders = []
    for i in range(expiry.MAX_CLOSURES + 4):
        order = await world.order()
        await age_order(order, 25 + i)
        orders.append(order)
    first = await run_expiry()
    assert len(first["closed"]) == expiry.MAX_CLOSURES
    assert (await reload(Order, orders[-1].id)).status == "cancelled"  # the oldest went first
    assert (await reload(Order, orders[0].id)).status == "pending"
    second = await run_expiry()
    assert len(second["closed"]) == 4


async def test_abandoned_no_response_case_is_closed_and_legacy_incident_withdrawn(world):
    order = await world.order(status="client_no_response", courier=world.courier, fee="5")
    long_ago = datetime.now(UTC) - timedelta(hours=60)
    async with SessionLocal() as s:
        s.add(NoResponseCase(order_id=order.id, status="waiting", started_at=long_ago, deadline_at=long_ago))
        s.add(
            NoResponseCase(
                order_id=order.id,
                status="resolved",
                started_at=long_ago,
                deadline_at=long_ago,
                resolved_at=long_ago,
                resolution="cancelled_kept",
                incident_counted=True,
                messaging_status="legacy",
            )
        )
        await s.commit()
    await age_order(order, 60)
    for case in await rows(select(NoResponseCase)):
        await age("no_response_cases", case.id, created_at=long_ago)
    result = await run_expiry()
    assert [c["reason"] for c in result["closed"]] == ["abandoned"]
    cases = {c.messaging_status: c for c in await rows(select(NoResponseCase))}
    assert (cases[None].status, cases[None].resolution, cases[None].incident_counted) == (
        "resolved",
        "auto_closed",
        False,
    )
    assert cases["legacy"].incident_counted is False and cases["legacy"].resolution == "cancelled_kept"


async def test_recent_no_response_step_keeps_the_order(world):
    order = await world.order(status="client_no_response", courier=world.courier, fee="5")
    await age_order(order, 60)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                status="waiting",
                started_at=datetime.now(UTC),
                deadline_at=datetime.now(UTC),
            )
        )
        await s.commit()
    assert (await run_expiry())["closed"] == []


async def test_orphan_offers(world):
    open_order = await world.order(status="offers_received")
    closed = await world.order(status="cancelled")
    taken = await world.order(status="accepted", courier=world.courier)
    kept = await world.offer(open_order)
    gone = [await world.offer(closed), await world.offer(taken, fee="9")]
    async with SessionLocal() as s:
        count = await expiry.expire_orphan_offers(s)
        await s.commit()
    assert count == 2
    assert (await reload(OrderOffer, kept.id)).status == "pending"
    assert {(await reload(OrderOffer, o.id)).status for o in gone} == {"expired"}


async def test_presence_expiry(client, world, factory):
    quiet_user = await factory.user(email="quiet@example.test", profile=False)
    quiet = await world.make_courier(quiet_user, last_seen_at=datetime.now(UTC) - timedelta(minutes=16))
    never_user = await factory.user(email="never@example.test", profile=False)
    never = await world.make_courier(never_user, last_seen_at=None)
    response = await client.post("/api/admin/jobs/courier_presence_expiry/run", headers=auth(world.admin))
    assert response.json()["result"] == {"set_offline": 2}
    assert (await reload(Courier, quiet.id)).is_online is False
    assert (await reload(Courier, never.id)).is_online is False
    assert (await reload(Courier, world.courier.id)).is_online is True
    # the online switch is a heartbeat
    r = await client.post(
        "/api/functions/updateMyCourierProfile",
        json={"fields": {"is_online": True}},
        headers=auth(quiet_user),
    )
    assert r.json()["profile"]["is_online"] is True
    assert (await reload(Courier, quiet.id)).last_seen_at > datetime.now(UTC) - timedelta(minutes=1)


async def test_order_jobs_are_registered_and_runnable(client, world):
    assert {
        "expire_stale_orders",
        "expire_orphan_offers",
        "courier_presence_expiry",
        "courier_statements",
    } <= set(JOBS)
    assert str(JOBS["courier_statements"].trigger.timezone) == "Africa/Tunis"
    for name, key in (("expire_stale_orders", "closed"), ("expire_orphan_offers", "offers_closed")):
        response = await client.post(
            f"/api/admin/jobs/{name}/run", headers={"x-cron-token": "test-cron-secret"}
        )
        assert response.json()["ok"] is True and key in response.json()["result"]
