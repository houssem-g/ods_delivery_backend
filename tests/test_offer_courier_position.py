"""Where the courier was when he offered (owner, 20/10/2026): stored on the offer (courier_lat / courier_lng /
courier_position_at, ~11 m), shown on the admin's « Voir le trajet » map only (getAdminOrderRoute) with the
course's steps from the status history. Never served to the customer or another courier. OSRM off."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.models import Courier, Notification, OrderOffer, OrderStatusEvent, OrderStockCheck
from tests.factories import auth
from tests.order_helpers import OrderWorld, now, pt, reload

FROM = (35.83127, 10.60043)  # ~1 km from the Sousse shop, distinctive digits
MOVED = (35.82871, 10.60519)


@pytest.fixture
async def world(factory, monkeypatch):
    monkeypatch.setattr(settings, "OSRM_URL", "")
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def place(courier, at, seen_minutes_ago=1.0):
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == courier.id)
            .values(
                last_location=pt(*at) if at else None,
                last_seen_at=now() - timedelta(minutes=seen_minutes_ago),
                location_at=now() - timedelta(minutes=seen_minutes_ago) if at else None,
            )
        )
        await s.commit()


def leaks(text: str) -> bool:
    return any(
        x in text for x in ("35.8313", "10.6004", "35.8287", "10.6052", "courier_lat", "courier_position")
    )


async def test_the_apps_position_is_stored_rounded_and_updated(client, world):
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "from_lat": FROM[0], "from_lng": FROM[1]},
    )
    assert sent.status_code == 200, sent.text
    offer_id = sent.json()["offer"]["id"]
    row = await reload(OrderOffer, offer_id)
    assert (row.courier_lat, row.courier_lng) == (Decimal("35.8313"), Decimal("10.6004"))
    assert abs((now() - row.courier_position_at).total_seconds()) < 60
    assert not leaks(sent.text)  # not even to himself

    moved = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": offer_id, "fee": 4.5, "from_lat": MOVED[0], "from_lng": MOVED[1]},
    )
    assert moved.status_code == 200 and not leaks(moved.text)
    row = await reload(OrderOffer, offer_id)
    assert (row.courier_lat, row.courier_lng) == (Decimal("35.8287"), Decimal("10.6052"))

    # a change without any usable position keeps the stored one (never an invented one)
    await place(world.courier, None)
    kept = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 5})
    assert kept.status_code == 200
    assert (await reload(OrderOffer, offer_id)).courier_lat == Decimal("35.8287")


async def test_the_servers_recent_position_else_nothing(client, world, factory):
    await place(world.courier, FROM, seen_minutes_ago=2)
    order = await world.order()
    sent = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 4})
    row = await reload(OrderOffer, sent.json()["offer"]["id"])
    assert row.courier_lat == Decimal("35.8313")
    assert abs((now() - timedelta(minutes=2) - row.courier_position_at).total_seconds()) < 30

    other_user = await factory.user(email="far@example.test", profile=False)
    other = await world.make_courier(other_user, display_name="Sami", at=None)
    blind = await call(client, other_user, "createOrderOffer", {"order_id": str(order.id), "fee": 4})
    assert blind.status_code == 200, blind.text
    row = await reload(OrderOffer, blind.json()["offer"]["id"])
    assert row.courier_lat is None and row.courier_lng is None and row.courier_position_at is None
    assert other.id == row.courier_id


async def test_abroad_is_never_stored(client, world):
    await place(world.courier, None)
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "from_lat": 48.85, "from_lng": 2.35},
    )
    assert (await reload(OrderOffer, sent.json()["offer"]["id"])).courier_lat is None


async def test_the_position_never_reaches_the_customer_or_another_courier(client, world, factory):
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "from_lat": FROM[0], "from_lng": FROM[1]},
    )
    offer_id = sent.json()["offer"]["id"]
    rival_user = await factory.user(email="rival@example.test", profile=False)
    await world.make_courier(rival_user, display_name="Rival")
    q = {"q": json.dumps({"order_id": str(order.id)})}
    seen = [
        await client.get(f"/api/entities/OrderOffer/{offer_id}", headers=auth(world.customer)),
        await client.get("/api/entities/OrderOffer", params=q, headers=auth(world.customer)),
        await client.get("/api/entities/OrderOffer", params=q, headers=auth(rival_user)),
        await client.get("/api/entities/Notification", headers=auth(world.customer)),
        await call(client, rival_user, "getOfferRank", {"order_id": str(order.id), "fee": 3}),
        await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id)}),
        await call(client, world.customer, "getOrderCourier", {"offer_id": offer_id}),
        await call(
            client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": offer_id}
        ),
        await client.get(f"/api/entities/OrderOffer/{offer_id}", headers=auth(world.customer)),
        await call(client, world.customer, "getAdminOrderRoute", {"order_id": str(order.id)}),
        await call(client, world.courier_user, "getAdminOrderRoute", {"order_id": str(order.id)}),
    ]
    assert seen[0].status_code == 200 and seen[0].json()["id"] == offer_id
    assert seen[7].status_code == 200 and seen[7].json()["success"], seen[7].text  # accepted: still nothing
    assert seen[-2].status_code == seen[-1].status_code == 403
    for r in seen:
        assert not leaks(r.text), r.text
    async with SessionLocal() as s:  # nor in any stored notification (push payloads included)
        for n in (await s.execute(Notification.__table__.select())).all():
            assert not leaks(str(dict(n._mapping)))

    admin = (await call(client, world.admin, "getAdminOrderRoute", {"order_id": str(order.id)})).json()
    assert admin["courier_position"]["lat"] == 35.8313 and admin["courier_position"]["lng"] == 10.6004


async def test_admin_route_has_the_courier_leg_and_the_steps(client, world, factory):
    order = await world.order(status="delivered", courier=world.courier)
    async with SessionLocal() as s:
        s.add(
            OrderOffer(
                order_id=order.id,
                courier_id=world.courier.id,
                proposed_fee=Decimal("5"),
                eta_minutes=20,
                status="accepted",
                courier_lat=Decimal("35.8313"),
                courier_lng=Decimal("10.6004"),
                courier_position_at=now() - timedelta(minutes=40),
            )
        )
        t0 = now() - timedelta(minutes=38)
        for minutes, status, where in (
            (0, "accepted", None),
            (9, "at_shop", (35.82561, 10.60842)),
            (21, "purchased", None),
            (22, "on_the_way", None),
            (35, "delivered", (35.83001, 10.62)),
        ):
            s.add(
                OrderStatusEvent(
                    order_id=order.id,
                    to_status=status,
                    source="test",
                    location=pt(*where) if where else None,
                    created_at=t0 + timedelta(minutes=minutes),
                )
            )
        s.add(
            OrderStockCheck(
                order_id=order.id,
                courier_id=world.courier.id,
                missing_text="Pain",
                deadline_at=t0 + timedelta(minutes=15),
                created_at=t0 + timedelta(minutes=12),
            )
        )
        await s.commit()
    body = (await call(client, world.admin, "getAdminOrderRoute", {"order_id": str(order.id)})).json()
    assert body["success"] is True
    assert body["courier_position"]["lat"] == 35.8313
    assert body["to_shop"]["distance_km"] > 0 and body["to_shop"]["duration_min"] > 0
    assert len(body["legs"]) == 1  # the course itself is unchanged: shop → customer
    assert [(st["key"], st["minutes"]) for st in body["steps"]] == [
        ("accepted", None),
        ("at_shop", 9),
        ("purchased", 12),
        ("on_the_way", 1),
        ("delivered", 13),
    ]
    assert body["steps"][1]["lat"] == 35.82561 and body["steps"][2]["lat"] is None
    assert [i["kind"] for i in body["incidents"]] == ["item_unavailable"]


async def test_older_offers_and_missing_steps(client, world):
    order = await world.order(status="accepted", courier=world.courier)
    await world.offer(order, status="accepted")  # no position recorded (before 20/10/2026)
    body = (await call(client, world.admin, "getAdminOrderRoute", {"order_id": str(order.id)})).json()
    assert body["courier_position"] is None and body["to_shop"] is None
    assert body["offer"]["to_shop_km"] is None
    steps = {st["key"]: st["at"] for st in body["steps"]}
    assert steps["accepted"] is not None and steps["at_shop"] is None and steps["delivered"] is None
    assert body["other_offers"] == [] and body["incidents"] == []
