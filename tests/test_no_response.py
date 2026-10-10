"""triggerEmergencyContact ("client ne répond pas"): every guard, the sweep job,
the cancelOrder hook, the NoResponseCase entity and the races."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.db import SessionLocal, transaction
from app.jobs import incidents as incident_jobs
from app.models import (
    Courier,
    HotDeal,
    NoResponseCase,
    Order,
    OrderStatusEvent,
    OrderTracking,
    OutboundMessage,
    User,
    customer_stats,
)
from app.services import cancellation, no_response
from app.services import order_transitions as ot
from tests.factories import auth
from tests.messaging_factories import ok_wa, sms_ok
from tests.order_helpers import OrderWorld, device, notifications, pushes, reload, rows, set_live

FN = "/api/functions/triggerEmergencyContact"


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.stranger = await factory.user(email="stranger@example.test")
    return w


async def call(client, user: User | None, action: str, order: Order | str | None) -> httpx.Response:
    body: dict[str, Any] = {"action": action}
    if order is not None:
        body["order_id"] = str(order.id) if isinstance(order, Order) else order
    return await client.post(FN, json=body, headers=auth(user) if user else {})


async def on_the_way(world, **fields: Any) -> Order:
    fields.setdefault("status", "on_the_way")
    fields.setdefault("items", "Lait x2")
    fields.setdefault("at_door", True)
    return await world.order(courier=world.courier, fee="5", purchase="25", **fields)


async def the_cases(order: Order) -> list[NoResponseCase]:
    return await rows(
        select(NoResponseCase).where(NoResponseCase.order_id == order.id).order_by(NoResponseCase.created_at)
    )


async def rewind(order: Order, seconds: float) -> None:
    """Time passes for the order's cases (their timestamps move back)."""
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


async def sweep_5min() -> dict[str, Any]:
    """The 5-minute sweep, service level (the job wrapper is tested in test_incident_jobs.py)."""
    async with transaction() as session:
        due = await no_response.due_order_ids(session)
    advanced = 0
    for order_id in due:
        async with transaction() as session:
            advanced += await no_response.sweep_order(session, order_id)
    return {"success": True, "checked": len(due), "advanced": advanced, "errors": 0}


async def incidents(user: User) -> int:
    async with SessionLocal() as s:
        count = (
            await s.execute(
                select(customer_stats.c.no_response_incidents).where(customer_stats.c.user_id == user.id)
            )
        ).scalar_one()
    return int(count)


async def set_status(order: Order, status: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(Order, order.id)
        row.status = status
        if status == "cancelled":
            row.cancelled_at = datetime.now(UTC)
        if status == "delivered":
            row.delivered_at = datetime.now(UTC)
        await s.commit()


async def doc(client, user: User, order: Order) -> dict[str, Any]:
    return (await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))).json()


def test_policy_values_match_the_front():
    # src/lib/noResponsePolicy.js
    assert no_response.WAIT_SECONDS == 180
    assert no_response.AUTO_CLOSE_HOURS == 3
    assert no_response.MAX_REPORTS == 2
    assert timedelta(minutes=2) == no_response.LEGACY_WAIT


async def test_report_parks_the_order_and_alerts_every_channel(client, world):
    order = await on_the_way(world)
    await device(world.customer)
    r = await call(client, world.courier_user, "report_no_response", order)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["message"] == "Emergency contact initiated"
    assert body["timeout_seconds"] == 180 and body["stage"] == "waiting"
    assert body["seconds_left"] in (179, 180)
    assert body["channels"] == {"in_app": True, "push_devices": 1, "whatsapp": "disabled", "sms": "disabled"}
    assert body["deadline_at"].endswith("Z") and body["can_resell"] is False
    [case] = await the_cases(order)
    assert (case.deadline_at - case.started_at) == timedelta(seconds=180)
    assert case.messaging_status == "whatsapp_disabled/sms_disabled" and case.courier_id == world.courier.id
    assert case.purchase_amount == Decimal("25")
    d = await doc(client, world.customer, order)
    assert d["status"] == "client_no_response" and d["no_response_reported"] is True
    assert d["no_response_case_id"] == str(case.id) and d["customer_responded_to_emergency"] is False
    assert d["no_response_channels"]["push_devices"] == 1
    assert d["status_history"][-1]["status"] == "client_no_response"
    assert d["status_history"][-1]["source"] == "triggerEmergencyContact"
    [alert] = await notifications(world.customer, "emergency_contact")
    assert "+21655123456" in alert.body_fr and "Karim T." in alert.body_fr  # one way to write the name (R30)
    assert alert.data["stage"] == "alert" and alert.data["case_id"] == str(case.id)
    assert len(await pushes(world.customer)) == 1
    [wa] = await rows(select(OutboundMessage).where(OutboundMessage.channel == "whatsapp"))
    assert wa.purpose == "customer_no_response" and wa.critical is True
    assert wa.params == ["Karim T.", "Monoprix", "+21655123456"]
    assert wa.idempotency_key == f"noresp:{case.id}" and wa.to_e164 == "+21622111222"


async def test_report_is_idempotent_while_open(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 60)
    again = (await call(client, world.courier_user, "report_no_response", order)).json()
    assert again["already_open"] is True and again["seconds_left"] in (119, 120)
    assert len(await the_cases(order)) == 1
    assert len(await notifications(world.customer)) == 1
    concurrent = await asyncio.gather(
        *(call(client, world.courier_user, "report_no_response", order) for _ in range(3))
    )
    assert all(r.status_code == 200 for r in concurrent)
    assert len(await the_cases(order)) == 1


async def test_guards(client, world):
    order = await on_the_way(world)
    assert (await call(client, world.customer, "report_no_response", order)).status_code == 403
    assert (await call(client, world.stranger, "status", order)).status_code == 403
    assert (await call(client, None, "status", order)).status_code == 401
    assert (await call(client, world.courier_user, "report_no_response", order)).status_code == 200
    assert (await call(client, world.courier_user, "customer_confirms", order)).status_code == 403
    assert (await call(client, world.customer, "courier_resume", order)).status_code == 403
    sweep = await call(client, world.admin, "sweep", order)
    assert sweep.status_code == 403 and sweep.json() == {"error": "Forbidden"}
    bogus = await call(client, world.courier_user, "bogus", order)
    assert bogus.status_code == 400 and bogus.json() == {"error": "Invalid action"}
    missing = await call(client, world.courier_user, "status", None)
    assert missing.status_code == 400 and missing.json() == {"error": "order_id is required"}
    for unknown in (str(uuid.uuid4()), "not-a-uuid"):
        r = await call(client, world.courier_user, "status", unknown)
        assert r.status_code == 404 and r.json() == {"error": "Order not found"}
    # the old action name and the admin
    assert (await call(client, world.customer, "check_response", order)).json()["stage"] == "waiting"
    assert (await call(client, world.admin, "status", order)).status_code == 200
    # another courier is a stranger
    other = await world.make_courier(await world.factory.user(email="c2@example.test"))
    assert other is not None
    assert (await call(client, world.stranger, "courier_resume", order)).status_code == 403


@pytest.mark.parametrize(
    "status", ["pending", "offers_received", "accepted", "at_shop", "delivered", "cancelled"]
)
async def test_not_reportable_before_the_goods_are_bought(client, world, status):
    courier = None if status in ("pending", "offers_received") else world.courier
    order = await world.order(status=status, courier=courier, purchase="25")
    r = await call(client, world.courier_user if courier else world.admin, "report_no_response", order)
    assert r.status_code == 409 and r.json() == {"error": "not_reportable", "status": status}
    assert await the_cases(order) == []


async def test_purchased_is_reportable(client, world):
    order = await on_the_way(world, status="purchased")
    assert (await call(client, world.courier_user, "report_no_response", order)).status_code == 200


async def test_timeout_enforced_once_by_whoever_polls_first(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 175)
    assert (await call(client, world.customer, "status", order)).json()["stage"] == "waiting"
    await rewind(order, 10)
    st = (await call(client, world.customer, "status", order)).json()  # the customer's screen
    assert st["stage"] == "expired" and st["can_resell"] is True and st["can_cancel_without_penalty"] is True
    assert st["seconds_left"] == 0
    await call(client, world.courier_user, "status", order)
    assert (await sweep_5min())["advanced"] == 0
    [case] = await the_cases(order)
    assert case.status == "expired" and case.incident_counted is True and case.final_at is not None
    assert await incidents(world.customer) == 1
    finals = await notifications(world.courier_user, "customer_no_response_final")
    last = [n for n in await notifications(world.customer, "emergency_contact") if n.data["stage"] == "final"]
    assert len(finals) == 1 and len(last) == 1 and last[0].data["incidents"] == 1
    assert finals[0].data["can_resell"] is True
    d = await doc(client, world.courier_user, order)
    assert d["no_response_final_at"] is not None and d["status"] == "client_no_response"


async def test_the_schedule_alone_finalizes_and_later_auto_closes(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await set_live(order, world.courier, 35.8, 10.6)
    await rewind(order, 200)
    summary = await sweep_5min()
    assert summary == {"success": True, "checked": 1, "advanced": 1, "errors": 0}
    assert (await the_cases(order))[0].status == "expired"
    assert (await sweep_5min())["advanced"] == 0  # idempotent
    await rewind(order, 3 * 3600)
    assert (await sweep_5min())["advanced"] == 1
    d = await doc(client, world.customer, order)
    assert d["status"] == "cancelled" and d["cancelled_by"] == "system"
    assert d["cancellation_reason"] == "client_no_response" and d["no_response_resolution"] == "auto_closed"
    assert [d["courier_live_lat"], d["courier_live_lng"], d["courier_live_at"]] == [None, None, None]
    assert await rows(select(OrderTracking)) == []
    [case] = await the_cases(order)
    assert case.resolution == "auto_closed" and case.incident_counted is True
    assert await incidents(world.customer) == 1
    [closed] = await notifications(world.customer, "delivery_cancelled")
    assert closed.data["reason"] == "client_no_response"
    auto = [n for n in await notifications(world.courier_user) if n.data.get("auto_closed")]
    assert len(auto) == 1
    assert (await sweep_5min())["checked"] == 0


async def test_the_customer_phone_rings_again_until_the_deadline(client, world):
    order = await on_the_way(world)
    await device(world.customer)
    await call(client, world.courier_user, "report_no_response", order)
    assert len(await pushes(world.customer)) == 1  # the alert
    await call(client, world.courier_user, "status", order)
    assert len(await pushes(world.customer)) == 1  # nothing due before 45 s
    await rewind(order, 50)
    assert (await incident_jobs.no_response_fast())["checked"] == 1
    await call(client, world.courier_user, "status", order)  # same look again: no duplicate
    assert len(await pushes(world.customer)) == 2
    await rewind(order, 45)
    await call(client, world.customer, "status", order)
    await rewind(order, 70)  # 165 s: reminder 3 only, reminder 2 is not replayed
    await incident_jobs.no_response_fast()
    sent = await pushes(world.customer)
    assert len(sent) == 4 and {p.payload["type"] for p in sent} == {"emergency_contact"}
    [case] = await the_cases(order)
    assert case.channels["reminders"] == 3 and case.channels["push_devices"] == 1
    # reminders are push only: the bell keeps the alert
    assert len(await notifications(world.customer, "emergency_contact")) == 1
    await rewind(order, 20)
    assert (await incident_jobs.no_response_fast())["advanced"] == 1  # deadline: last chance, on time
    assert (await the_cases(order))[0].status == "expired"
    assert len(await pushes(world.customer)) == 5
    assert await incident_jobs.no_response_fast() is None  # nothing waiting any more


async def test_no_reminder_once_the_customer_answered(client, world):
    order = await on_the_way(world)
    await device(world.customer)
    await call(client, world.courier_user, "report_no_response", order)
    await call(client, world.customer, "customer_confirms", order)
    await rewind(order, 100)
    assert await incident_jobs.no_response_fast() is None
    assert len(await pushes(world.customer)) == 1


async def test_customer_answers_in_time(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 30)
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "Customer confirmed availability" and body["stage"] == "resolved"
    assert body["customer_responded"] is True and body["order_status"] == "on_the_way"
    d = await doc(client, world.customer, order)
    assert d["status"] == "on_the_way" and d["customer_responded_to_emergency"] is True
    assert d["customer_responded_at"] is not None
    assert d["status_history"][-1]["source"] == "customer_confirmed"
    [case] = await the_cases(order)
    assert case.resolution == "customer_confirmed" and case.incident_counted is False
    assert case.customer_answered_late is False
    [told] = await notifications(world.courier_user, "customer_responded")
    assert "Amel Ben Ali" in told.body_fr
    again = await call(client, world.customer, "customer_confirms", order)
    assert again.status_code == 200 and again.json()["already"] is True


async def test_late_answer_voids_the_incident(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await call(client, world.courier_user, "status", order)
    assert await incidents(world.customer) == 1
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 200
    assert (await reload(Order, order.id)).status == "on_the_way"
    [case] = await the_cases(order)
    assert case.incident_counted is False and case.customer_answered_late is True
    assert await incidents(world.customer) == 0


async def test_answer_after_the_resale_is_too_late(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await set_status(order, "cancelled")
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 409
    assert r.json() == {"error": "too_late", "reason": "cancelled", "status": "cancelled", "resolution": None}
    assert (await reload(Order, order.id)).status == "cancelled"
    await set_status(order, "delivered")
    assert (await call(client, world.courier_user, "courier_resume", order)).json()["reason"] == "delivered"


async def test_race_safety_net_a_listed_deal_wins(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await set_live(order, world.courier, 35.8, 10.6)
    await rewind(order, 200)
    async with SessionLocal() as s:
        s.add(
            HotDeal(
                original_order_id=order.id,
                courier_id=world.courier.id,
                items_text="Lait x2",
                purchase_amount=Decimal("25"),
                discount_percentage=Decimal("0"),
                price=Decimal("25"),
                delivery_fee=Decimal("3"),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
        )
        await s.commit()
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 409 and r.json()["error"] == "too_late" and r.json()["reason"] == "resold"
    d = await doc(client, world.customer, order)
    assert d["status"] == "cancelled" and d["no_response_resolution"] == "resold"
    assert d["courier_live_lat"] is None and d["status_history"][-1]["reason"] == "resold"
    [case] = await the_cases(order)
    assert case.status == "resolved" and case.incident_counted is True


async def test_courier_reached_the_customer_himself(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await call(client, world.courier_user, "status", order)
    r = await call(client, world.courier_user, "courier_resume", order)
    assert r.status_code == 200 and r.json()["resolution"] == "courier_reached"
    d = await doc(client, world.customer, order)
    assert d["status"] == "on_the_way" and d["customer_responded_to_emergency"] is True
    assert d["status_history"][-1]["source"] == "courier_reached"
    assert (await the_cases(order))[0].resolution == "courier_reached"
    assert await incidents(world.customer) == 0
    assert await notifications(world.courier_user, "customer_responded") == []


async def test_at_most_two_reports_per_order(client, world):
    order = await on_the_way(world)
    for _ in range(2):
        assert (await call(client, world.courier_user, "report_no_response", order)).status_code == 200
        await call(client, world.customer, "customer_confirms", order)
    third = await call(client, world.courier_user, "report_no_response", order)
    assert third.status_code == 409 and third.json() == {"error": "too_many_reports", "max": 2}


async def test_a_courier_cannot_pile_up_incidents(client, world):
    order = await on_the_way(world)
    for _ in range(5):  # hammering before the deadline: one case, no incident
        await call(client, world.courier_user, "report_no_response", order)
        await call(client, world.courier_user, "status", order)
        await rewind(order, 10)
    assert len(await the_cases(order)) == 1
    assert (await the_cases(order))[0].incident_counted is False
    assert await incidents(world.customer) == 0
    await rewind(order, 200)  # deadline → resume → report → deadline: the resumed case is voided
    await call(client, world.courier_user, "status", order)
    await call(client, world.courier_user, "courier_resume", order)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await call(client, world.courier_user, "status", order)
    cases = await the_cases(order)
    assert len(cases) == 2 and sum(c.incident_counted for c in cases) == 1
    assert await incidents(world.customer) == 1
    again = await call(client, world.courier_user, "report_no_response", order)
    assert again.status_code == 200 and again.json()["already_open"] is True
    assert (await call(client, world.courier_user, "realert_no_response", order)).json() == {
        "error": "too_many_reports",
        "max": 2,
    }
    assert len(await the_cases(order)) == 2


async def test_second_urgent_alert_after_the_deadline_then_resale(client, world):
    order = await on_the_way(world)
    await device(world.customer)
    await call(client, world.courier_user, "report_no_response", order)
    early = await call(client, world.courier_user, "realert_no_response", order)
    assert early.status_code == 409 and early.json()["error"] == "not_expired"
    assert (await call(client, world.courier_user, "status", order)).json()["can_realert"] is False
    await rewind(order, 200)
    st = (await call(client, world.courier_user, "status", order)).json()
    assert st["stage"] == "expired" and st["can_realert"] is True and st["reports"] == 1
    assert st["max_reports"] == 2 and await incidents(world.customer) == 1
    assert (await call(client, world.customer, "realert_no_response", order)).status_code == 403
    r = await call(client, world.courier_user, "realert_no_response", order)
    body = r.json()
    assert r.status_code == 200 and body["stage"] == "waiting" and body["seconds_left"] in (179, 180)
    assert body["can_resell"] is False and body["can_realert"] is False
    first, second = await the_cases(order)
    assert first.resolution == "realerted" and first.incident_counted is False
    # the incident moves to the last alert and stays counted (QA 06/10 N3: « déjà enregistré »)
    assert second.status == "waiting" and second.incident_counted is True
    assert await incidents(world.customer) == 1
    sent = await notifications(world.customer, "emergency_contact")
    alerts = [n for n in sent if n.data["stage"] == "alert"]
    assert [n.data["attempt"] for n in alerts] == [1, 2] and "Dernier appel" in alerts[1].title_fr
    d = await doc(client, world.customer, order)
    assert d["status"] == "client_no_response" and d["no_response_case_id"] == str(second.id)
    await rewind(order, 60)
    await incident_jobs.no_response_fast()  # reminders run again
    assert (await the_cases(order))[1].channels["reminders"] == 1
    await rewind(order, 200)
    st = (await call(client, world.courier_user, "status", order)).json()
    assert st["stage"] == "expired" and st["can_resell"] is True and st["can_realert"] is False
    assert await incidents(world.customer) == 1  # one incident for the order, not two
    third = await call(client, world.courier_user, "realert_no_response", order)
    assert third.status_code == 409 and third.json()["error"] == "too_many_reports"


async def test_customer_answering_the_second_alert_voids_the_incident(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await call(client, world.courier_user, "realert_no_response", order)
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 200
    assert await incidents(world.customer) == 0
    assert [c.resolution for c in await the_cases(order)] == ["realerted", "customer_confirmed"]


async def test_answers_need_an_open_case(client, world):
    order = await on_the_way(world, status="client_no_response")
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                status="resolved",
                resolution="resold",
                started_at=datetime.now(UTC) - timedelta(hours=1),
                deadline_at=datetime.now(UTC) - timedelta(minutes=57),
            )
        )
        await s.commit()
    r = await call(client, world.customer, "customer_confirms", order)
    assert r.status_code == 409 and r.json() == {"error": "no_open_case", "status": "client_no_response"}
    assert (await reload(Order, order.id)).status == "client_no_response"
    assert (await call(client, world.courier_user, "courier_resume", order)).json()["error"] == "no_open_case"
    # nothing reported: answering changes nothing
    plain = await on_the_way(world)
    ok = await call(client, world.customer, "customer_confirms", plain)
    assert ok.status_code == 200 and ok.json()["already"] is True and ok.json()["stage"] == "none"
    assert await the_cases(plain) == []
    assert len(await rows(select(OrderStatusEvent).where(OrderStatusEvent.order_id == plain.id))) == 2


async def test_qa_orders_never_trigger_whatsapp(client, world):
    order = await on_the_way(world, items="QA TEST lait")
    body = (await call(client, world.courier_user, "report_no_response", order)).json()
    assert body["channels"]["whatsapp"] == "skipped_test"
    assert await rows(select(OutboundMessage)) == []
    assert (await the_cases(order))[0].messaging_status == "whatsapp_skipped_test"


async def test_legacy_parked_order_gets_a_case_never_an_incident(client, world):
    order = await on_the_way(world, status="client_no_response")
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE order_status_events SET created_at = :w WHERE order_id = :o AND to_status = :s"),
            {"w": datetime.now(UTC) - timedelta(seconds=130), "o": order.id, "s": "client_no_response"},
        )
        await s.commit()
    st = (await call(client, world.courier_user, "status", order)).json()
    assert st["stage"] == "expired" and st["can_resell"] is True  # the courier can still resell / cancel
    [case] = await the_cases(order)
    assert case.messaging_status == "legacy" and case.incident_counted is False
    assert (case.deadline_at - case.started_at) == timedelta(minutes=2)
    assert await notifications(world.customer, "emergency_contact") == []
    assert await incidents(world.customer) == 0


async def test_legacy_case_counted_by_the_former_code_is_voided_at_auto_close(client, world):
    order = await on_the_way(world, status="client_no_response")
    started = datetime.now(UTC) - timedelta(hours=5)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=order.id,
                status="expired",
                messaging_status="legacy",
                incident_counted=True,
                started_at=started,
                deadline_at=started + timedelta(minutes=2),
                final_at=started + timedelta(minutes=2),
            )
        )
        await s.commit()
    assert await incidents(world.customer) == 1
    st = (await call(client, world.customer, "status", order)).json()
    assert st["stage"] == "resolved" and st["resolution"] == "auto_closed"
    assert (await reload(Order, order.id)).status == "cancelled"
    assert await incidents(world.customer) == 0


async def test_legacy_parked_months_ago_is_closed_at_once(client, world):
    order = await on_the_way(world, status="client_no_response")
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE order_status_events SET created_at = :w WHERE order_id = :o"),
            {"w": datetime.now(UTC) - timedelta(days=200), "o": order.id},
        )
        await s.commit()
    assert (await sweep_5min())["advanced"] == 1
    d = await doc(client, world.customer, order)
    assert d["status"] == "cancelled" and d["no_response_resolution"] == "auto_closed"
    assert (await the_cases(order))[0].incident_counted is False


async def test_whatsapp_not_delivered_asks_for_the_sms(client, world, http, meta_on, sms_on):
    http.responder = lambda request, n: ok_wa() if "graph" in str(request.url) else sms_ok()
    order = await on_the_way(world)
    body = (await call(client, world.courier_user, "report_no_response", order)).json()
    assert body["channels"]["whatsapp"] == "sent" and body["channels"]["sms"] is None
    assert len(http.calls) == 1
    await rewind(order, 30)
    await call(client, world.courier_user, "status", order)
    assert len(http.calls) == 1  # not before 60 s
    await rewind(order, 35)
    async with SessionLocal() as s:  # the WhatsApp row's own fallback deadline is past too
        await s.execute(
            text("UPDATE outbound_messages SET fallback_deadline_at = now() - interval '1 second'")
        )
        await s.commit()
    st = (await call(client, world.courier_user, "status", order)).json()
    assert len(http.calls) == 2 and st["channels"]["sms"] == "sent"
    await call(client, world.courier_user, "status", order)
    assert len(http.calls) == 2
    assert (await the_cases(order))[0].channels["sms"] == "sent"


async def test_whatsapp_failure_is_recorded_not_fatal(client, world, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("meta down")

    monkeypatch.setattr(no_response.whatsapp, "send_template", broken)
    order = await on_the_way(world)
    r = await call(client, world.courier_user, "report_no_response", order)
    assert r.status_code == 200 and r.json()["channels"]["whatsapp"] == "error"


async def test_an_order_closed_outside_the_procedure_closes_its_case(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await set_status(order, "cancelled")
    await rewind(order, 200)
    await call(client, world.customer, "status", order)
    [case] = await the_cases(order)
    assert case.status == "resolved" and case.resolution == "order_cancelled"
    assert case.incident_counted is False
    # expired then cancelled elsewhere: the incident already counted is kept
    other = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", other)
    await rewind(other, 200)
    await call(client, world.courier_user, "status", other)
    await set_status(other, "cancelled")
    assert (await sweep_5min())["advanced"] == 1
    [kept] = await the_cases(other)
    assert kept.resolution == "cancelled_kept" and kept.incident_counted is True
    # delivered meanwhile: the incident is withdrawn
    third = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", third)
    await rewind(third, 200)
    await call(client, world.courier_user, "status", third)
    assert await incidents(world.customer) == 2
    await set_status(third, "delivered")
    await call(client, world.customer, "status", third)
    [done] = await the_cases(third)
    assert done.resolution == "delivered" and done.incident_counted is False
    assert await incidents(world.customer) == 1


async def test_a_case_left_open_is_closed_before_a_new_report(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await set_status(order, "on_the_way")  # moved on outside the procedure
    await rewind(order, 30)
    st = (await call(client, world.courier_user, "status", order)).json()
    assert st["stage"] == "waiting"  # an open case on an order that moved: left as is
    assert (await call(client, world.courier_user, "report_no_response", order)).status_code == 200
    first, second = await the_cases(order)
    assert first.resolution == "customer_confirmed" and first.incident_counted is False
    assert second.status == "waiting"


async def test_no_suspension_any_more(client, world):
    """Owner, 10/10/2026: never suspended; the old automatic flag is cleared at the next change."""
    async with SessionLocal() as s:
        (await s.get(User, world.customer.id)).is_blacklisted = True
        await s.commit()
    for _ in range(5):
        order = await on_the_way(world)
        await call(client, world.courier_user, "report_no_response", order)
        await rewind(order, 200)
        await call(client, world.courier_user, "status", order)
    assert await incidents(world.customer) == 5
    assert (await reload(User, world.customer.id)).is_blacklisted is False
    profile = (
        await client.get(f"/api/entities/UserProfile/{world.customer.id}", headers=auth(world.customer))
    ).json()
    assert profile["no_response_incidents"] == 5 and profile["is_blacklisted"] is False


# --- cancelOrder hook -----------------------------------------------------------------------------


async def test_cancel_order_refreshes_the_case_first(client, world):
    assert cancellation.no_response_refresh is no_response.refresh
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)  # deadline past, nobody polled
    r = await client.post(
        "/api/functions/cancelOrder",
        json={
            "order_id": str(order.id),
            "reason": "client_no_response",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
        headers=auth(world.courier_user),
    )
    assert r.status_code == 200
    [case] = await the_cases(order)
    assert case.resolution == "cancelled_kept" and case.incident_counted is True
    assert (await reload(Order, order.id)).status == "cancelled"
    assert (await reload(Courier, world.courier.id)).late_cancellations == 0
    assert await incidents(world.customer) == 1
    assert len(await notifications(world.courier_user, "customer_no_response_final")) == 1


async def test_cancel_order_on_a_legacy_case_counts_nothing(client, world):
    order = await on_the_way(world, status="client_no_response")
    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE order_status_events SET created_at = now() - interval '10 minutes' "
                "WHERE order_id = :o"
            ),
            {"o": order.id},
        )
        await s.commit()
    r = await client.post(
        "/api/functions/cancelOrder",
        json={
            "order_id": str(order.id),
            "reason": "goods_returned_to_shop",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
        headers=auth(world.courier_user),
    )
    assert r.status_code == 200
    [case] = await the_cases(order)
    assert case.resolution == "returned_to_shop" and case.incident_counted is False
    assert await incidents(world.customer) == 0


# --- races ------------------------------------------------------------------------------------------


async def test_race_sweep_against_the_customer_answer(client, world):
    for _ in range(3):
        order = await on_the_way(world)
        await call(client, world.courier_user, "report_no_response", order)
        await rewind(order, 200)
        swept, answered = await asyncio.gather(
            sweep_5min(), call(client, world.customer, "customer_confirms", order)
        )
        assert swept["errors"] == 0 and answered.status_code == 200
        [case] = await the_cases(order)
        assert case.status == "resolved" and case.resolution == "customer_confirmed"
        assert case.incident_counted is False
        assert (await reload(Order, order.id)).status == "on_the_way"
        finals = [
            n
            for n in await notifications(world.customer, "emergency_contact")
            if n.order_id == order.id and n.data.get("stage") == "final"
        ]
        # the sweep came first (incident recorded, then voided by the late answer) or not at all
        assert len(finals) == (1 if case.customer_answered_late else 0)
    assert await incidents(world.customer) == 0


async def test_race_two_pollers_record_the_incident_once(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await asyncio.gather(
        call(client, world.courier_user, "status", order),
        call(client, world.customer, "status", order),
        sweep_5min(),
    )
    assert await incidents(world.customer) == 1
    assert len(await notifications(world.courier_user, "customer_no_response_final")) == 1


# --- NoResponseCase entity ------------------------------------------------------------------------


async def test_no_response_case_entity(client, world):
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    [case] = await the_cases(order)
    url = f"/api/entities/NoResponseCase/{case.id}"
    for reader in (world.customer, world.courier_user, world.admin):
        r = await client.get(url, headers=auth(reader))
        assert r.status_code == 200
        d = r.json()
        assert d["order_id"] == str(order.id) and d["customer_id"] == "cust@example.test"
        assert d["courier_user_id"] == "courier@example.test" and d["courier_id"] == str(world.courier.id)
        assert d["status"] == "waiting" and d["push_devices"] == 0 and d["incident_counted"] is False
    assert (await client.get(url, headers=auth(world.stranger))).status_code == 404
    listed = await client.get(
        "/api/entities/NoResponseCase",
        params={"q": f'{{"order_id":"{order.id}"}}'},
        headers=auth(world.stranger),
    )
    assert listed.json() == []
    assert (
        await client.patch(url, json={"status": "resolved"}, headers=auth(world.admin))
    ).status_code == 403
    assert (await client.delete(url, headers=auth(world.courier_user))).status_code == 403
    created = await client.post(
        "/api/entities/NoResponseCase", json={"order_id": str(order.id)}, headers=auth(world.admin)
    )
    assert created.status_code == 403


async def test_every_change_emits_case_and_order_events(client, world, monkeypatch):
    seen: list[tuple[str, str]] = []
    original = no_response.emit

    def record(session, entity, type_, id_, audience=None):
        seen.append((entity, type_))
        original(session, entity, type_, id_, audience)

    monkeypatch.setattr(no_response, "emit", record)
    monkeypatch.setattr(ot, "emit", record)
    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    assert ("NoResponseCase", "create") in seen and ("Order", "update") in seen
    seen.clear()
    await rewind(order, 200)
    await call(client, world.customer, "status", order)
    assert ("NoResponseCase", "update") in seen and ("Order", "update") in seen
    seen.clear()
    await call(client, world.customer, "customer_confirms", order)
    assert ("NoResponseCase", "update") in seen and ("Order", "update") in seen
