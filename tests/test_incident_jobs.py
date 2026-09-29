"""The incident jobs: sweep_5min (no-response sweep), hourly_cleanup (hot-deal expiry) and the
"hot_deals" step of test_data_purge. Idempotency, isolation of failures, counters."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.jobs import incidents
from app.jobs import messaging as messaging_jobs
from app.jobs.messaging import PURGE_STEPS
from app.jobs.registry import JOBS
from app.models import HotDeal, NoResponseCase, Order
from app.services import hot_deals, no_response
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, reload, rows


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def parked(client, world) -> Order:
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="25")
    r = await client.post(
        "/api/functions/triggerEmergencyContact",
        json={"order_id": str(order.id), "action": "report_no_response"},
        headers=auth(world.courier_user),
    )
    assert r.status_code == 200
    return order


async def rewind(order: Order, seconds: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE no_response_cases SET started_at = started_at - make_interval(secs => :s), "
                "deadline_at = deadline_at - make_interval(secs => :s), "
                "final_at = final_at - make_interval(secs => :s) WHERE order_id = :o"
            ),
            {"s": seconds, "o": order.id},
        )
        await s.commit()


async def deal(world, *, expires_in: timedelta, **fields: Any) -> HotDeal:
    source = await world.order(status="cancelled", courier=world.courier, purchase="20")
    values: dict[str, Any] = {
        "original_order_id": source.id,
        "courier_id": world.courier.id,
        "items_text": "Pain",
        "purchase_amount": Decimal("20"),
        "discount_percentage": Decimal("0"),
        "price": Decimal("20"),
        "delivery_fee": Decimal("3"),
        "expires_at": datetime.now(UTC) + expires_in,
    }
    values.update(fields)
    async with SessionLocal() as s:
        row = HotDeal(**values)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


def test_jobs_are_registered_with_their_triggers():
    assert JOBS["sweep_5min"].func is incidents.sweep_5min
    assert JOBS["sweep_5min"].trigger.interval == timedelta(minutes=5)
    assert JOBS["hourly_cleanup"].trigger.interval == timedelta(hours=1)
    assert JOBS["no_response_fast"].func is incidents.no_response_fast
    assert JOBS["no_response_fast"].trigger.interval == timedelta(seconds=15)
    assert PURGE_STEPS["hot_deals"] is incidents.purge_hot_deals


async def test_sweep_finalizes_then_auto_closes_idempotently(client, world):
    order = await parked(client, world)
    assert await incidents.sweep_5min() == {"success": True, "checked": 1, "advanced": 0, "errors": 0}
    await rewind(order, 200)
    assert (await incidents.sweep_5min())["advanced"] == 1
    assert (await incidents.sweep_5min())["advanced"] == 0
    assert len(await notifications(world.courier_user, "customer_no_response_final")) == 1
    await rewind(order, 3 * 3600)
    assert (await incidents.sweep_5min())["advanced"] == 1
    assert (await reload(Order, order.id)).status == "cancelled"
    assert await incidents.sweep_5min() == {"success": True, "checked": 0, "advanced": 0, "errors": 0}


async def test_sweep_one_bad_order_does_not_block_the_others(client, world, monkeypatch):
    bad, good = await parked(client, world), await parked(client, world)
    await rewind(bad, 200)
    await rewind(good, 200)
    real = no_response.sweep_order

    async def flaky(session, order_id):
        if order_id == bad.id:
            raise RuntimeError("boom")
        return await real(session, order_id)

    monkeypatch.setattr(no_response, "sweep_order", flaky)
    summary = await incidents.sweep_5min()
    assert summary["checked"] == 2 and summary["advanced"] == 1 and summary["errors"] == 1
    [bad_case] = await rows(select(NoResponseCase).where(NoResponseCase.order_id == bad.id))
    assert bad_case.status == "waiting"  # its transaction rolled back


async def test_sweep_skips_an_order_someone_holds(client, world):
    order = await parked(client, world)
    await rewind(order, 200)
    async with SessionLocal() as holder:
        await holder.execute(select(Order).where(Order.id == order.id).with_for_update())
        summary = await incidents.sweep_5min()
        await holder.rollback()
    assert summary["checked"] == 1 and summary["advanced"] == 0
    assert (await incidents.sweep_5min())["advanced"] == 1


async def test_hourly_cleanup_expires_listed_deals_once(world, monkeypatch):
    seen: list[tuple[str, str]] = []
    original = hot_deals.emit
    monkeypatch.setattr(
        hot_deals,
        "emit",
        lambda s, e, t, i, audience=None: (seen.append((e, t)), original(s, e, t, i, audience)),
    )
    old = await deal(world, expires_in=timedelta(minutes=-5))
    fresh = await deal(world, expires_in=timedelta(hours=1))
    sold = await deal(world, expires_in=timedelta(minutes=-5), status="sold", buyer_id=world.customer.id)
    assert await incidents.hourly_cleanup() == {"expired_deals": 1}
    assert seen == [("ResaleOrder", "delete")]
    assert (await reload(HotDeal, old.id)).status == "expired"
    assert (await reload(HotDeal, fresh.id)).status == "available"
    assert (await reload(HotDeal, sold.id)).status == "sold"
    assert await incidents.hourly_cleanup() == {"expired_deals": 0}


async def test_purge_step_deletes_expired_unsold_and_qa_deals(world):
    expired = await deal(world, expires_in=timedelta(minutes=-5), status="expired")
    listed_past = await deal(world, expires_in=timedelta(minutes=-5))
    sold = await deal(world, expires_in=timedelta(minutes=-5), status="sold", buyer_id=world.customer.id)
    live = await deal(world, expires_in=timedelta(hours=1))
    qa_live = await deal(world, expires_in=timedelta(hours=1), items_text="QA TEST pain")
    buyer_order = await world.order(status="delivered", courier=world.courier, purchase="20")
    qa_sold = await deal(
        world,
        expires_in=timedelta(minutes=-5),
        items_text="PW-123 pain",
        status="sold",
        buyer_id=world.customer.id,
        buyer_order_id=buyer_order.id,
    )
    async with SessionLocal() as s:
        row = await s.get(Order, buyer_order.id)
        row.resale_deal_id = qa_sold.id
        await s.commit()
    metrics = await messaging_jobs.test_data_purge()
    assert metrics["expired_deals_deleted"] == 2 and metrics["test_run_deals_deleted"] == 1
    assert metrics["failed_steps"] == 0 and "hot_deals" in metrics["steps"]
    left = {d.id for d in await rows(select(HotDeal))}
    assert left == {sold.id, live.id, qa_live.id}
    assert expired.id not in left and listed_past.id not in left
    assert (await reload(Order, buyer_order.id)).resale_deal_id is None
    again = await messaging_jobs.test_data_purge()
    assert again["expired_deals_deleted"] == 0 and again["test_run_deals_deleted"] == 0


async def test_jobs_run_by_hand(client, world):
    await deal(world, expires_in=timedelta(minutes=-1))
    r = await client.post("/api/admin/jobs/hourly_cleanup/run", headers=auth(world.admin))
    assert r.json()["ok"] is True and r.json()["result"] == {"expired_deals": 1}
