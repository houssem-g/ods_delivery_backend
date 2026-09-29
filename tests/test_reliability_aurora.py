"""getCustomerReliability's Aurora extras: delivered_orders, reliability_pct, avg_reply_seconds."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db import SessionLocal
from app.models import Message, NoResponseCase
from tests.factories import auth
from tests.order_helpers import OrderWorld


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await client.post("/api/functions/getCustomerReliability", json=payload or {}, headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


async def chat(world, order, *lines: tuple[str, float, bool]) -> None:
    """(sender_role, seconds after a base time, template)."""
    base = datetime.now(UTC) - timedelta(hours=1)
    async with SessionLocal() as s:
        for role, seconds, template in lines:
            sender = world.customer if role == "customer" else world.courier_user
            s.add(
                Message(
                    order_id=order.id, sender_id=sender.id, sender_role=role, body="x",
                    is_template=template, created_at=base + timedelta(seconds=seconds),
                )
            )  # fmt: skip
        await s.commit()


async def test_a_new_customer_is_fully_reliable(client, world):
    body = await call(client, world.customer)
    assert body["delivered_orders"] == 0 and body["reliability_pct"] == 100
    assert body["avg_reply_seconds"] is None


async def test_reliability_pct_and_reply_time(client, world):
    for _ in range(3):
        await world.order(status="delivered", courier=world.courier, fee="5")
    cancelled = await world.order(status="cancelled")
    when = datetime.now(UTC) - timedelta(days=2)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=cancelled.id, status="resolved", started_at=when, deadline_at=when, final_at=when,
                incident_counted=True,
            )
        )  # fmt: skip
        await s.commit()
    first = await world.order(status="on_the_way", courier=world.courier, fee="5")
    await chat(
        world,
        first,
        ("courier", 0, False),
        ("courier", 10, False),  # same turn: one sample, from the first message
        ("courier", 20, True),  # a template line never counts
        ("customer", 60, False),  # → 60 s
        ("customer", 70, False),
        ("courier", 100, False),
        ("customer", 130, False),  # → 30 s
        ("courier", 200, False),  # never answered: no sample
    )
    body = await call(client, world.customer)
    assert body["delivered_orders"] == 3 and body["reliability_pct"] == 75
    assert body["avg_reply_seconds"] is None  # 2 samples only
    second = await world.order(status="delivered", courier=world.courier, fee="5")
    await chat(
        world, second, ("customer", 0, False), ("courier", 5, False), ("customer", 305, False)
    )  # → 300 s
    body = await call(client, world.customer)
    assert body["delivered_orders"] == 4 and body["reliability_pct"] == 80
    assert body["avg_reply_seconds"] == 60  # median of 60, 30, 300

    # the courier asking about an order of this customer gets the same figures
    order = await world.order()
    seen = await call(client, world.courier_user, {"order_id": str(order.id)})
    assert seen["incidents"] == 1
    assert (seen["delivered_orders"], seen["reliability_pct"], seen["avg_reply_seconds"]) == (4, 80, 60)


async def test_old_chats_do_not_count(client, world):
    order = await world.order(status="delivered", courier=world.courier, fee="5")
    old = datetime.now(UTC) - timedelta(days=91)
    async with SessionLocal() as s:
        for n in range(3):
            for role, seconds in (("courier", 0), ("customer", 10)):
                s.add(
                    Message(
                        order_id=order.id,
                        sender_id=(world.customer if role == "customer" else world.courier_user).id,
                        sender_role=role,
                        body="x",
                        created_at=old + timedelta(minutes=n, seconds=seconds),
                    )
                )
        await s.commit()
    assert (await call(client, world.customer))["avg_reply_seconds"] is None
