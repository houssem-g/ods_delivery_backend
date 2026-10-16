"""Signed webhooks to the partner restaurants (Lot 2, 2B): the queue, the signature, the events
of the order flow, the retries and the test ping."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal, transaction
from app.jobs import partners as partner_jobs
from app.jobs.registry import JOBS
from app.models import PartnerWebhookDelivery
from app.services import partner_webhooks
from tests.factories import auth
from tests.order_helpers import OrderWorld, reload, rows
from tests.partner_helpers import (
    PARTNER_API,
    WEBHOOK_URL,
    WebhookServer,
    make_partner,
    order_body,
    partner_order,
    post_order,
)


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
def server():
    return WebhookServer().install()


@pytest.fixture
async def pw(client, world):
    return await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)


async def fn(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def deliveries(order_id=None) -> list[PartnerWebhookDelivery]:
    stmt = select(PartnerWebhookDelivery).order_by(
        PartnerWebhookDelivery.created_at, PartnerWebhookDelivery.id
    )
    if order_id is not None:
        stmt = stmt.where(PartnerWebhookDelivery.order_id == order_id)
    return await rows(stmt)


async def run_job() -> dict:
    async with transaction() as session:
        return await partner_webhooks.deliver_due(session)


async def due_now() -> None:
    async with SessionLocal() as s:
        await s.execute(text("UPDATE partner_webhook_deliveries SET next_attempt_at = now()"))
        await s.commit()


async def step(client, world, order_id, body):
    r = await client.patch(f"/api/entities/Order/{order_id}", json=body, headers=auth(world.courier_user))
    assert r.status_code == 200, r.text
    return r


# --- signature --------------------------------------------------------------------------------------


def test_signature_binds_the_timestamp_and_the_raw_body():
    body = b'{"event":"ping"}'
    header = partner_webhooks.sign("s3cret", 1_800_000_000, body)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    assert partner_webhooks.verify("s3cret", "1800000000", body, header, now=1_800_000_100)
    assert not partner_webhooks.verify("s3cret", "1800000000", body + b" ", header, now=1_800_000_100)
    assert not partner_webhooks.verify("other", "1800000000", body, header, now=1_800_000_100)
    assert not partner_webhooks.verify("s3cret", "1800000001", body, header, now=1_800_000_100)  # ts inside
    assert not partner_webhooks.verify("s3cret", "1800000000", body, header, now=1_800_000_301)  # ± 5 min
    assert not partner_webhooks.verify("s3cret", "x", body, header)


def test_transition_events_mapping():
    ev = partner_webhooks.transition_events
    assert ev("pending", "offers_received", None, None) == []
    assert ev("offers_received", "pending", None, None) == []  # last offer withdrawn
    assert ev("accepted", "pending", "sick", "courier") == [
        ("order.cancelled", {"cancelled_by": "courier", "reason": "sick", "back_to_pool": True})
    ]
    assert ev("offers_received", "accepted", None, None) == [("order.accepted", {})]
    assert ev("accepted", "at_shop", None, None) == [("order.at_shop", {})]
    assert ev("at_shop", "price_confirmation_needed", None, None) == []
    assert ev("purchased", "on_the_way", None, None) == [("order.on_the_way", {"resumed": False})]
    assert ev("client_no_response", "on_the_way", "customer_confirmed", None) == [
        ("order.on_the_way", {"resumed": True})
    ]
    assert ev("on_the_way", "client_no_response", None, None) == [("order.no_response", {})]
    assert ev("on_the_way", "delivered", None, None) == [("order.delivered", {})]
    assert ev("pending", "cancelled", "expired_no_offer", "system") == [
        ("order.expired", {"reason": "expired_no_offer"})
    ]
    assert ev("accepted", "cancelled", "plat raté", "customer") == [
        ("order.cancelled", {"cancelled_by": "customer", "reason": "plat raté", "back_to_pool": False})
    ]
    assert ev("client_no_response", "cancelled", "goods_returned_to_shop", "courier") == [
        (
            "order.cancelled",
            {"cancelled_by": "courier", "reason": "goods_returned_to_shop", "back_to_pool": False},
        ),
        ("order.returned_to_shop", {}),
    ]


def test_the_job_is_registered():
    job = JOBS["deliver_partner_webhooks"]
    assert job.func is partner_jobs.deliver_partner_webhooks and job.enabled


# --- the flow ---------------------------------------------------------------------------------------


async def test_a_local_offer_sends_a_signed_webhook_to_the_partner_server(client, world, pw, server):
    created = (await post_order(client, pw)).json()
    assert await deliveries() == []  # the creation itself is the partner's own call
    offer = (
        await fn(
            client,
            world.courier_user,
            "createOrderOffer",
            {"order_id": created["order_id"], "fee": 5, "eta_minutes": 12, "message": "J'arrive"},
        )
    ).json()["offer"]
    [row] = await deliveries()
    assert row.event == "order.offer_received" and row.status == "pending" and row.attempts == 0
    assert row.payload["data"]["offer"]["fee"] == 5 and "order" not in row.payload["data"]
    assert server.calls == []

    assert await run_job() == {"due": 1, "delivered": 1, "failed": 0}
    [call] = server.calls
    assert str(call.request.url) == WEBHOOK_URL and call.request.method == "POST"
    assert call.headers["X-ODSD-Event"] == "order.offer_received"
    assert call.headers["X-ODSD-Delivery-Id"] == str(row.id)
    assert abs(int(call.headers["X-ODSD-Timestamp"]) - datetime.now(UTC).timestamp()) < 30
    assert call.headers["content-type"] == "application/json"
    assert server.verify(call, pw.webhook_secret) is True
    assert not server.verify(call, "wrong-secret")
    body = call.body
    assert body["id"] == str(row.id) and body["event"] == "order.offer_received"
    assert body["partner_ref"] == "ods-1-4321" and body["order_id"] == created["order_id"]
    assert body["created_at"].endswith("Z")
    assert body["data"]["offer"]["offer_id"] == offer["id"] and body["data"]["offer"]["eta_minutes"] == 12
    assert body["data"]["offer"]["courier"] == {
        "first_name": "Karim",
        "rating": 5,
        "total_deliveries": 0,
        "vehicle": "scooter",
    }
    assert body["data"]["order"]["status"] == "offers_received" and body["data"]["order"]["offers_count"] == 1
    assert body["data"]["order"]["customer_reliability"]["visible"] is False
    row = await reload(PartnerWebhookDelivery, row.id)
    assert row.status == "delivered" and row.attempts == 1 and row.last_status == 200 and row.delivered_at
    assert await run_job() == {"due": 0, "delivered": 0, "failed": 0}  # nothing twice


async def test_every_step_of_the_delivery_reaches_the_partner(client, world, pw, server):
    order_id = (await post_order(client, pw)).json()["order_id"]
    offer_id = (
        await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    ).json()["offer"]["id"]
    changed = await fn(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 4.5})
    assert changed.status_code == 200, changed.text
    await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    await step(client, world, order_id, {"status": "at_shop"})
    await step(client, world, order_id, {"status": "purchased", "purchase_amount": 8.8})
    await step(client, world, order_id, {"status": "on_the_way"})
    await step(client, world, order_id, {"status": "delivered"})

    events = [d.event for d in await deliveries()]
    assert events == [
        "order.offer_received",
        "order.offer_updated",
        "order.accepted",
        "order.at_shop",
        "order.purchased",
        "order.on_the_way",
        "order.delivered",
    ]
    updated = next(d for d in await deliveries() if d.event == "order.offer_updated")
    assert updated.payload["data"]["previous_fee"] == 5 and updated.payload["data"]["offer"]["fee"] == 4.5
    accepted = next(d for d in await deliveries() if d.event == "order.accepted")
    assert accepted.payload["data"]["delivery_fee"] == 4.5
    assert await run_job() == {"due": 7, "delivered": 7, "failed": 0}
    assert server.events() == events
    assert all(server.verify(c, pw.webhook_secret) for c in server.calls)
    last = server.calls[-1].body["data"]["order"]
    assert (
        last["status"] == "delivered"
        and last["courier_state"] == "delivered"
        and last["purchase_amount"] == 8.8
    )
    assert last["delivered_at"] and last["courier"]["phone"] == "+21655123456"


async def test_withdrawn_offer_and_courier_drop_and_partner_cancel(client, world, pw, server, factory):
    order_id = (await post_order(client, pw)).json()["order_id"]
    offer_id = (
        await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    ).json()["offer"]["id"]
    gone = await client.delete(f"/api/entities/OrderOffer/{offer_id}", headers=auth(world.courier_user))
    assert gone.status_code in (200, 204), gone.text
    assert [d.event for d in await deliveries()] == ["order.offer_received", "order.offer_withdrawn"]
    assert (await deliveries())[-1].payload["data"] == {"offer": {"offer_id": offer_id}}

    second_user = await factory.user(email="second@example.test", profile=False)
    second = await world.make_courier(second_user, display_name="Nour")
    offer_id = (await fn(client, second_user, "createOrderOffer", {"order_id": order_id, "fee": 6})).json()[
        "offer"
    ]["id"]
    await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    dropped = await fn(
        client,
        second_user,
        "cancelOrder",
        {"order_id": order_id, "reason": "panne", "cancelled_by": "courier", "courier_id": str(second.id)},
    )
    assert dropped.status_code == 200, dropped.text
    back = next(d for d in await deliveries() if d.event == "order.cancelled")
    assert back.payload["data"] == {"cancelled_by": "courier", "reason": "panne", "back_to_pool": True}
    view = (await client.get(f"{PARTNER_API}/orders/{order_id}", headers=pw.headers)).json()
    assert view["status"] == "pending" and view["courier_state"] == "searching" and view["courier"] is None

    cancelled = await client.post(
        f"{PARTNER_API}/orders/{order_id}/cancel", json={"reason": "plus besoin"}, headers=pw.headers
    )
    assert cancelled.status_code == 200
    final = [d for d in await deliveries() if d.event == "order.cancelled"][-1]
    assert final.payload["data"] == {
        "cancelled_by": "customer",
        "reason": "plus besoin",
        "back_to_pool": False,
    }
    await run_job()
    assert server.events()[-1] == "order.cancelled"
    assert server.calls[-1].body["data"]["order"]["cancelled_by"] == "customer"


async def test_no_response_event_carries_the_deadline(client, world, pw, server):
    order = await partner_order(
        world, pw, status="on_the_way", courier=world.courier, fee="4", purchase="20", at_door=True
    )
    r = await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"order_id": str(order.id), "action": "report_no_response"},
    )
    assert r.status_code == 200
    [row] = await deliveries(order.id)
    assert row.event == "order.no_response"
    assert row.payload["data"]["wait_seconds"] in (180, 600) and row.payload["data"]["deadline_at"].endswith(
        "Z"
    )
    r = await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"order_id": str(order.id), "action": "courier_resume"},
    )
    assert r.status_code == 200
    assert [d.event for d in await deliveries(order.id)] == ["order.no_response", "order.on_the_way"]
    assert (await deliveries(order.id))[-1].payload["data"] == {"resumed": True}


# --- retries ----------------------------------------------------------------------------------------


async def test_failures_are_retried_1_2_4_8_16_minutes_with_the_same_id_then_failed(
    client, world, pw, server
):
    server.status = 503
    order_id = (await post_order(client, pw)).json()["order_id"]
    await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    [row] = await deliveries()
    expected_gaps = [1, 2, 4, 8, 16]
    for attempt, gap in enumerate(expected_gaps, start=1):
        before = datetime.now(UTC)
        assert await run_job() == {"due": 1, "delivered": 0, "failed": 1}
        row = await reload(PartnerWebhookDelivery, row.id)
        assert row.status == "pending" and row.attempts == attempt and row.last_status == 503
        assert row.last_error == "http_503"
        assert (
            timedelta(minutes=gap) - timedelta(seconds=5)
            <= row.next_attempt_at - before
            <= timedelta(minutes=gap, seconds=5)
        )
        assert await run_job() == {"due": 0, "delivered": 0, "failed": 0}  # not before its time
        await due_now()
    server.status = 650  # connection refused on the last attempt
    assert await run_job() == {"due": 1, "delivered": 0, "failed": 1}
    row = await reload(PartnerWebhookDelivery, row.id)
    assert row.status == "failed" and row.attempts == 6 and row.last_error == "ConnectError"
    assert {c.headers["X-ODSD-Delivery-Id"] for c in server.calls} == {str(row.id)} and len(server.calls) == 6
    assert await run_job() == {"due": 0, "delivered": 0, "failed": 0}
    listed = (
        await fn(client, world.admin, "listPartnerWebhookDeliveries", {"partner_id": str(pw.partner.id)})
    ).json()
    [entry] = listed["deliveries"]
    assert entry["status"] == "failed" and entry["attempts"] == 6 and entry["last_error"] == "ConnectError"
    assert entry["event"] == "order.offer_received" and entry["next_attempt_at"] is None


async def test_a_success_after_a_failure_closes_the_row(client, world, pw, server):
    server.status = 500
    order_id = (await post_order(client, pw)).json()["order_id"]
    await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    await run_job()
    await due_now()
    server.status = 204
    assert await run_job() == {"due": 1, "delivered": 1, "failed": 0}
    [row] = await deliveries()
    assert (
        row.status == "delivered" and row.attempts == 2 and row.last_status == 204 and row.last_error is None
    )


async def test_without_webhook_url_nothing_is_queued(client, world, server):
    silent = await make_partner(client, world.admin, name="Silent")
    order_id = (await post_order(client, silent)).json()["order_id"]
    await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    assert await deliveries() == []
    r = await client.post(f"{PARTNER_API}/webhook-test", headers=silent.headers)
    assert r.status_code == 409 and r.json()["error"] == "webhook_not_configured"


async def test_http_urls_are_refused_outside_local(client, world, pw, server, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    r = await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)
    assert r.status_code == 200 and r.json()["delivered"] is False and r.json()["error"] == "https_required"
    assert server.calls == []


# --- ping -------------------------------------------------------------------------------------------


async def test_webhook_test_and_test_partner_webhook_send_a_signed_ping_now(client, world, pw, server):
    r = await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True and r.json()["status_code"] == 200 and r.json()["error"] is None
    [call] = server.calls
    assert call.headers["X-ODSD-Event"] == "ping" and server.verify(call, pw.webhook_secret)
    assert call.body["event"] == "ping" and call.body["order_id"] is None and call.body["partner_ref"] is None
    assert call.body["data"] == {"partner": {"id": str(pw.partner.id), "name": "Coffee and Tea"}}

    server.status = 500
    r = (await fn(client, world.admin, "testPartnerWebhook", {"partner_id": str(pw.partner.id)})).json()
    assert (
        r["success"] is True
        and r["delivered"] is False
        and r["status_code"] == 500
        and r["error"] == "http_500"
    )
    rows_ = await deliveries()
    assert [d.event for d in rows_] == ["ping", "ping"] and rows_[-1].status == "pending"

    rotated = (
        await fn(client, world.admin, "rotatePartnerWebhookSecret", {"partner_id": str(pw.partner.id)})
    ).json()
    assert rotated["webhook_secret"] != pw.webhook_secret
    server.status = 200
    await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)
    assert server.verify(server.calls[-1], rotated["webhook_secret"]) and not server.verify(
        server.calls[-1], pw.webhook_secret
    )


async def test_partner_customer_stats_snapshot_is_refreshed_on_terminal_statuses(client, world, pw, server):
    from app.models import PartnerCustomerStat
    from app.services.partner_stats import phone_hash

    order_id = (await post_order(client, pw)).json()["order_id"]
    offer_id = (
        await fn(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    ).json()["offer"]["id"]
    await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    assert await rows(select(PartnerCustomerStat)) == []
    await step(client, world, order_id, {"status": "at_shop"})
    await step(client, world, order_id, {"status": "purchased", "purchase_amount": 8.8})
    await step(client, world, order_id, {"status": "on_the_way"})
    await step(client, world, order_id, {"status": "delivered"})
    [stat] = await rows(select(PartnerCustomerStat))
    assert stat.phone_hash == phone_hash("+21622111222")
    assert (stat.total_orders, stat.delivered_orders, stat.recent_orders, stat.recent_unpaid) == (1, 1, 1, 0)
    assert order_body()["customer"]["phone"] == "22 111 222"
