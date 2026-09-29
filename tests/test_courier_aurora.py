"""Aurora courier profile: vehicle, plate, daily goal, time online, ratings count and acceptance
rate; the courier's vehicle on the customer's Order and the offer card's counters."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update

from app.db import SessionLocal, transaction
from app.models import Courier, Order, OrderRating, OrderStatusEvent
from app.services import couriers as courier_service
from tests.factories import auth
from tests.order_helpers import OrderWorld, now, reload


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def update_profile(client, world, **fields: Any):
    return await call(
        client, world.courier_user, "updateMyCourierProfile", {"action": "update", "fields": fields}
    )


async def set_courier(courier, **values: Any) -> None:
    async with SessionLocal() as s:
        await s.execute(update(Courier).where(Courier.id == courier.id).values(**values))
        await s.commit()


async def profile(client, user, courier) -> dict[str, Any]:
    r = await client.get(f"/api/entities/CourierProfile/{courier.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


async def test_vehicle_plate_and_goal(client, world):
    r = await update_profile(
        client, world, vehicle_model="  Peugeot   Kisbee ", vehicle_plate=" 123 tu  4567 ", daily_goal="80.5"
    )
    assert r.status_code == 200, r.text
    doc = r.json()["profile"]
    assert doc["vehicle_model"] == "Peugeot Kisbee" and doc["vehicle_plate"] == "123 TU 4567"
    assert doc["daily_goal"] == 80.5
    row = await reload(Courier, world.courier.id)
    assert row.daily_goal == Decimal("80.500")
    cleared = await update_profile(client, world, vehicle_plate="", vehicle_model="")
    assert (
        cleared.json()["profile"]["vehicle_plate"] is None
        and cleared.json()["profile"]["vehicle_model"] is None
    )
    for bad in (
        {"vehicle_plate": "X" * 21},
        {"vehicle_model": "M" * 61},
        {"daily_goal": 10001},
        {"daily_goal": -1},
    ):
        r = await update_profile(client, world, **bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_fields", bad
    # owner and admin only (the entity's rule)
    assert (
        await client.get(f"/api/entities/CourierProfile/{world.courier.id}", headers=auth(world.customer))
    ).status_code == 404
    assert (await profile(client, world.admin, world.courier))["vehicle_plate"] is None


async def test_time_online_is_counted_per_tunis_day(client, world):
    await set_courier(world.courier, is_online=False, online_since=None)
    r = await update_profile(client, world, is_online=True)
    first = (await reload(Courier, world.courier.id)).online_since
    assert r.status_code == 200 and first is not None
    await update_profile(client, world, is_online=True)  # heartbeat: the session goes on
    assert (await reload(Courier, world.courier.id)).online_since == first
    await set_courier(world.courier, online_since=now() - timedelta(minutes=10))
    doc = await profile(client, world.courier_user, world.courier)
    assert 595 <= doc["online_seconds_today"] <= 610 and doc["online_since"] is not None

    await update_profile(client, world, is_online=False)
    row = await reload(Courier, world.courier.id)
    assert row.online_since is None and 595 <= row.online_seconds <= 610
    assert row.online_day == courier_service.tunis_day(now())
    doc = await profile(client, world.courier_user, world.courier)
    assert doc["online_since"] is None and doc["online_seconds_today"] == row.online_seconds

    # a stored count of another day is not today's
    await set_courier(world.courier, online_day=row.online_day - timedelta(days=1), online_seconds=5000)
    assert (await profile(client, world.courier_user, world.courier))["online_seconds_today"] == 0
    await set_courier(world.courier, is_online=True, online_since=now() - timedelta(seconds=30))
    await update_profile(client, world, is_online=False)
    row = await reload(Courier, world.courier.id)
    assert 29 <= row.online_seconds <= 40 and row.online_day == courier_service.tunis_day(now())


def test_a_session_started_yesterday_counts_from_midnight():
    tz = courier_service.TUNIS
    courier = Courier(online_since=datetime(2026, 9, 28, 23, 0, tzinfo=tz), online_day=None, online_seconds=0)
    at = datetime(2026, 9, 29, 0, 30, tzinfo=tz)
    courier_service.stop_online(courier, at.astimezone(UTC))
    assert courier.online_seconds == 1800 and courier.online_day == datetime(2026, 9, 29).date()
    assert courier.online_since is None
    courier_service.stop_online(courier, at)  # nothing running: unchanged
    assert courier.online_seconds == 1800
    courier.online_since = at - timedelta(minutes=5)
    courier_service.stop_online(courier, at)
    assert courier.online_seconds == 2100


async def test_presence_expiry_closes_the_session_at_the_last_heartbeat(world):
    await set_courier(
        world.courier,
        is_online=True,
        online_since=now() - timedelta(minutes=20),
        last_seen_at=now() - timedelta(minutes=16),
        online_day=courier_service.tunis_day(now()),
        online_seconds=100,
    )
    async with transaction() as session:
        assert await courier_service.expire_presence(session) == 1
    row = await reload(Courier, world.courier.id)
    assert row.is_online is False and row.online_since is None
    assert 100 + 235 <= row.online_seconds <= 100 + 245  # 4 minutes online, not 20
    assert row.online_day == courier_service.tunis_day(now())


async def test_ratings_count_and_acceptance_rate(client, world, factory):
    doc = await profile(client, world.courier_user, world.courier)
    assert doc["ratings_count"] == 0 and doc["acceptance_rate"] is None
    done = [await world.order(status="delivered", courier=world.courier, fee="5") for _ in range(2)]
    await world.order(status="on_the_way", courier=world.courier, fee="5")
    async with SessionLocal() as s:
        for order in done:
            s.add(
                OrderRating(
                    order_id=order.id, courier_id=world.courier.id, rater_id=world.customer.id, rating=5
                )
            )
        await s.commit()
    assert (await profile(client, world.courier_user, world.courier))["acceptance_rate"] == 100
    # one delivery he gave up (back to the pool, no longer his) and one he cancelled outright
    dropped = await world.order(status="pending")
    cancelled = await world.order(status="cancelled", courier=world.courier, fee="5")
    old = await world.order(status="delivered", courier=world.courier)
    async with SessionLocal() as s:  # assigned 100 days ago: out of the window
        await s.execute(
            update(Order).where(Order.id == old.id).values(accepted_at=now() - timedelta(days=100))
        )
        await s.commit()
    async with SessionLocal() as s:
        for order, to_status in ((dropped, "pending"), (cancelled, "cancelled")):
            s.add(
                OrderStatusEvent(
                    order_id=order.id, from_status="accepted", to_status=to_status, source="cancelOrder",
                    cancelled_by="courier", actor_user_id=world.courier_user.id,
                )
            )  # fmt: skip
        # another courier's cancellation is not his
        s.add(
            OrderStatusEvent(
                order_id=old.id, to_status="pending", source="cancelOrder", cancelled_by="courier",
                actor_user_id=world.customer.id,
            )
        )  # fmt: skip
        await s.commit()
    doc = await profile(client, world.courier_user, world.courier)
    assert doc["ratings_count"] == 2
    assert doc["acceptance_rate"] == 60  # 5 assigned in 90 days, 2 cancelled by him


async def test_customer_sees_the_couriers_vehicle_while_assigned(client, world, factory):
    await set_courier(world.courier, vehicle_model="Kisbee", vehicle_plate="123 TU 4567")
    order = await world.order(status="on_the_way", courier=world.courier, fee="5")
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["courier_vehicle_model"] == "Kisbee" and doc["courier_plate"] == "123 TU 4567"
    assert doc["courier_rating_count"] == 0
    admin = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.admin))).json()
    assert admin["courier_plate"] == "123 TU 4567"
    mine = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert mine["courier_plate"] is None  # the customer's field
    done = await world.order(status="delivered", courier=world.courier, fee="5")
    past = (await client.get(f"/api/entities/Order/{done.id}", headers=auth(world.customer))).json()
    assert past["courier_plate"] is None and past["courier_vehicle_model"] is None


async def test_offer_card_counters_for_the_customer(client, world, factory):
    for _ in range(2):
        await world.order(status="delivered", courier=world.courier, fee="5")
    other = await factory.user(email="o@example.test")
    await world.order(other, status="delivered", courier=world.courier, fee="5")
    order = await world.order(status="offers_received")
    offer = await world.offer(order)
    doc = (await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.customer))).json()
    assert doc["courier_deliveries"] == 3 and doc["delivered_to_you"] == 2
    assert doc["courier_ratings_count"] == 0 and doc["courier_vehicle"] == "scooter"
    own = (await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))).json()
    assert own["delivered_to_you"] is None and own["courier_deliveries"] is None


async def test_account_deletion_clears_the_plate(client, world):
    await set_courier(world.courier, vehicle_model="Kisbee", vehicle_plate="123 TU 4567", online_since=now())
    r = await client.post(
        "/api/functions/deleteMyAccount", json={"confirm": True}, headers=auth(world.courier_user)
    )
    assert r.status_code == 200, r.text
    row = await reload(Courier, world.courier.id)
    assert row.vehicle_plate is None and row.vehicle_model is None and row.online_since is None
