"""signalOfferIntent and Order.preparing_offers ("un livreur prépare une offre…")."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.jobs.orders import expire_orphan_offers
from app.models import OfferIntent, OrderStatusEvent
from app.services import offer_intents
from tests.factories import auth
from tests.order_helpers import OrderWorld, now, rows


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.rival_user = await factory.user(email="rival@example.test", profile=False)
    w.rival = await w.make_courier(w.rival_user, display_name="Sami")
    return w


@pytest.fixture
def order_events(monkeypatch) -> list[str]:
    seen: list[str] = []
    original = offer_intents.emit

    def record(session, entity, type_, id_, audience=None):
        seen.append(f"{entity}:{type_}:{id_}")
        original(session, entity, type_, id_, audience)

    monkeypatch.setattr(offer_intents, "emit", record)
    return seen


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def intent(client, user, order, active: bool = True):
    return await call(client, user, "signalOfferIntent", {"order_id": str(order.id), "active": active})


async def preparing(client, user, order) -> Any:
    return (await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))).json()[
        "preparing_offers"
    ]


async def age_intents(minutes: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE offer_intents SET updated_at = now() - make_interval(mins => :m)"), {"m": minutes}
        )
        await s.commit()


async def test_intents_are_counted_for_the_customer(client, world, order_events):
    order = await world.order()
    r = await intent(client, world.courier_user, order)
    assert r.status_code == 200 and r.json() == {"success": True, "active": True, "ttl_seconds": 180}
    assert order_events == [f"Order:update:{order.id}"]
    await intent(client, world.courier_user, order)  # a refresh: the count does not change
    assert len(order_events) == 1
    await intent(client, world.rival_user, order)
    assert await preparing(client, world.customer, order) == 2
    assert await preparing(client, world.admin, order) == 2
    assert await preparing(client, world.courier_user, order) is None  # the customer's field

    # a stale intent no longer counts; refreshing it announces it again
    await age_intents(4)
    assert await preparing(client, world.customer, order) == 0
    await intent(client, world.courier_user, order)
    assert len(order_events) == 3 and await preparing(client, world.customer, order) == 1

    # closing the sheet
    closed = await intent(client, world.courier_user, order, active=False)
    assert closed.json() == {"success": True, "active": False}
    assert await preparing(client, world.customer, order) == 0
    assert await rows(select(OfferIntent)) != []  # the rival's (stale) row stays until the purge


async def test_sending_the_offer_ends_the_intent(client, world):
    order = await world.order()
    await intent(client, world.courier_user, order)
    await intent(client, world.rival_user, order)
    await world.offer(order, world.rival)  # a pending offer counts as an offer, not an intent
    assert await preparing(client, world.customer, order) == 1
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 200, r.text
    assert await preparing(client, world.customer, order) == 0
    assert [i.courier_id for i in await rows(select(OfferIntent))] == [world.rival.id]


async def test_intent_refusals(client, world, factory):
    order = await world.order()
    cases = [
        (world.courier_user, {"active": True}, 400, "Missing order_id"),
        (world.courier_user, {"order_id": str(order.id), "active": "yes"}, 400, "invalid_active"),
        (world.customer, {"order_id": str(order.id), "active": True}, 403, "courier_profile_missing"),
        (world.courier_user, {"order_id": "nope", "active": True}, 404, "order_not_found"),
    ]
    for user, payload, status, error in cases:
        r = await call(client, user, "signalOfferIntent", payload)
        assert (r.status_code, r.json()["error"]) == (status, error), payload
    pending_user = await factory.user(email="p@example.test", profile=False)
    await world.make_courier(pending_user, verification="pending")
    assert (await intent(client, pending_user, order)).json()["error"] == "courier_not_verified"
    own = await world.order(world.courier_user)
    assert (await intent(client, world.courier_user, own)).json()["error"] == "own_order"
    taken = await world.order(status="accepted", courier=world.rival)
    r = await intent(client, world.courier_user, taken)
    assert r.status_code == 409 and r.json() == {"error": "order_not_open", "status": "accepted"}
    # closing always works (the order may have been taken meanwhile)
    assert (await intent(client, world.courier_user, taken, active=False)).status_code == 200
    async with SessionLocal() as s:
        s.add(
            OrderStatusEvent(
                order_id=order.id, to_status="pending", source="cancelOrder", cancelled_by="courier",
                actor_user_id=world.courier_user.id,
            )
        )  # fmt: skip
        await s.commit()
    assert (await intent(client, world.courier_user, order)).json()["error"] == "order_dropped"
    assert await rows(select(OfferIntent)) == []


async def test_intent_rate_limit(client, world):
    order = await world.order()
    for _ in range(30):
        assert (await intent(client, world.courier_user, order)).status_code == 200
    r = await intent(client, world.courier_user, order)
    assert r.status_code == 429 and r.json()["error"] == "too_many_intent_signals"


async def test_hourly_job_purges_old_intents(client, world):
    order = await world.order()
    await intent(client, world.courier_user, order)
    await intent(client, world.rival_user, order)
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE offer_intents SET updated_at = :t WHERE courier_id = :c"),
            {"t": now() - timedelta(minutes=11), "c": world.courier.id},
        )
        await s.commit()
    summary = await expire_orphan_offers()
    assert summary == {"offers_closed": 0, "offer_intents_deleted": 1}
    assert [i.courier_id for i in await rows(select(OfferIntent))] == [world.rival.id]
