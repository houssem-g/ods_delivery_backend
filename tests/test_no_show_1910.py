"""« Le client n'est pas venu » (owner, 19/10/2026, QA M6): the customer answered both alerts but does
not come. Last wait (3 min, rule D3), at the door (≤ 300 m), then a verified no-response: no penalty for
the courier, an incident for the customer, never a third alert, then the usual choices."""

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.models import Courier, Order
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, reload, set_live
from tests.test_no_response import call, incidents, on_the_way, the_cases


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture(autouse=True)
def outside_a_prayer(monkeypatch):
    """The 3-minute cases must not depend on the hour the suite runs (at a real prayer the last wait is
    10 min); the prayer test sets its own slot."""
    from app.services import prayer_times

    monkeypatch.setattr(prayer_times, "slow_slot", lambda now, where=prayer_times.SOUSSE: None)


async def answered_both(client, world, order) -> None:
    """Two alerts, the customer answers « j'arrive » to both (back on the way each time)."""
    for _ in range(2):
        assert (await call(client, world.courier_user, "report_no_response", order)).status_code == 200
        assert (await call(client, world.customer, "customer_confirms", order)).status_code == 200


async def time_passes(order, seconds: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE no_response_cases SET started_at = started_at - make_interval(secs => :s), "
                "deadline_at = deadline_at - make_interval(secs => :s), "
                "resolved_at = resolved_at - make_interval(secs => :s) WHERE order_id = :o"
            ),
            {"s": seconds, "o": order.id},
        )
        await s.commit()


async def policy(client, world, order) -> dict:
    r = await client.post(
        "/api/functions/getCancellationPolicy",
        json={"order_id": str(order.id), "actor": "courier"},
        headers=auth(world.courier_user),
    )
    return r.json()["policy"]


async def late(world) -> int:
    return (await reload(Courier, world.courier.id)).late_cancellations


async def test_no_show_after_the_last_wait_at_the_door(client, world):
    order = await on_the_way(world)
    await answered_both(client, world, order)
    # the courier is told what comes next when the customer answers the last alert
    told = (await notifications(world.courier_user, "customer_responded"))[-1]
    assert "Le client n'est pas venu" in told.body_fr and told.data["no_show_after"]

    # before the last wait is over: refused, the policy says how long is left
    early = await call(client, world.courier_user, "customer_no_show", order)
    assert early.status_code == 409 and early.json()["error"] == "wait"
    assert 170 <= early.json()["seconds_left"] <= 180
    p = await policy(client, world, order)
    assert p["can_resell"] is False and p["can_declare_no_show"] is False
    # verif prod 19/10 (P2): the words never freeze a duration, the screen counts from no_show
    assert p["no_show"]["max_distance_m"] == 300 and "compte à rebours" in p["message_fr"]
    assert " min " not in p["message_fr"] and "دق" not in p["message_ar"]
    assert 170 <= p["no_show"]["seconds_left"] <= 180 and p["no_show"]["wait_until"]
    status = (await call(client, world.courier_user, "status", order)).json()
    assert status["no_show"]["ready"] is False and status["can_realert"] is False

    await time_passes(order, 200)
    p = await policy(client, world, order)
    assert p["can_declare_no_show"] is True and "Le client n'est pas venu" in p["message_fr"]
    # too far from the door: refused (the alert's own position check)
    await set_live(order, world.courier, 35.8600, 10.6500)
    far = await call(client, world.courier_user, "customer_no_show", order)
    assert far.status_code == 409 and far.json()["error"] == "too_far"
    assert (await call(client, world.customer, "customer_no_show", order)).status_code == 403

    await set_live(order, world.courier, 35.8300, 10.6200)  # back at the door
    before = await late(world)
    r = await call(client, world.courier_user, "customer_no_show", order)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["no_show_recorded"] is True and body["no_show"] is None  # the last wait is behind
    assert body["stage"] == "expired" and body["can_resell"] is True and body["can_realert"] is False
    assert (await reload(Order, order.id)).status == "client_no_response"
    assert await incidents(world.customer) == 1  # counted like a verified no-response (badge)
    *_, case = await the_cases(order)
    assert case.messaging_status == "no_show" and case.incident_counted and case.status == "expired"
    told = (await notifications(world.customer, "emergency_contact"))[-1]
    assert told.data["stage"] == "no_show" and "pas venu" in told.title_fr
    # verif prod 19/10 (P3): a shop order is resold or kept, never « rapporté »
    assert told.data["restaurant_order"] is False and told.data["courier_options"] == ["resell", "keep"]
    assert "rapporter" not in told.body_fr

    # never a third alert (R36)
    again = await call(client, world.courier_user, "realert_no_response", order)
    assert again.status_code == 409 and again.json()["error"] == "too_many_reports"
    # the policy: everything free now
    p = await policy(client, world, order)
    assert p["can_resell"] is True and p["penalty_applies"] is False and p["no_response_verified"] is True
    assert "Sans pénalité" in p["message_fr"]
    # a shop order: Offre Chaude allowed (no more 409 customer_answered)
    deal = await client.post(
        "/api/functions/createHotDeal", json={"order_id": str(order.id)}, headers=auth(world.courier_user)
    )
    assert deal.status_code == 200, deal.text
    assert await late(world) == before  # no penalty at any step
    assert await incidents(world.customer) == 1


async def test_no_show_then_keep_the_goods_without_penalty(client, world):
    order = await on_the_way(world)
    await answered_both(client, world, order)
    await time_passes(order, 200)
    assert (await call(client, world.courier_user, "customer_no_show", order)).status_code == 200
    before = await late(world)
    r = await client.post(
        "/api/functions/cancelOrder",
        json={
            "order_id": str(order.id),
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
            "reason": "client_no_response",
        },
        headers=auth(world.courier_user),
    )
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).status == "cancelled" and await late(world) == before
    assert await incidents(world.customer) == 1


async def test_no_show_of_a_vitrine_order_goes_back_to_the_restaurant(client, world):
    from tests.test_vitrine_orders import public_vitrine

    shop_id, _ids = await public_vitrine(client, world.admin)
    order = await on_the_way(world, source="vitrine", vitrine_shop_id=shop_id)
    await answered_both(client, world, order)
    await time_passes(order, 200)
    r = await call(client, world.courier_user, "customer_no_show", order)
    assert r.status_code == 200, r.text
    assert r.json()["return_deadline_at"] is not None  # 30 min to bring the meal back (16/10)
    p = await policy(client, world, order)
    assert p["restaurant_order"] is True and "restaurant" in p["message_fr"] and p["penalty_applies"] is False
    before = await late(world)
    back = await client.post(
        "/api/functions/cancelOrder",
        json={
            "order_id": str(order.id),
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
            "reason": "goods_returned_to_shop",
        },
        headers=auth(world.courier_user),
    )
    assert back.status_code == 200, back.text
    assert (await reload(Order, order.id)).status == "cancelled" and await late(world) == before
    *_, case = await the_cases(order)
    assert case.resolution == "returned_to_shop" and case.incident_counted is True


async def test_no_show_needs_the_last_alert_answered(client, world):
    order = await on_the_way(world)
    # nothing reported: not applicable
    none = await call(client, world.courier_user, "customer_no_show", order)
    assert none.status_code == 409 and none.json()["error"] == "no_show_not_applicable"
    # one alert answered: a second alert is still possible, the no-show is not
    await call(client, world.courier_user, "report_no_response", order)
    await call(client, world.customer, "customer_confirms", order)
    await time_passes(order, 600)
    one = await call(client, world.courier_user, "customer_no_show", order)
    assert one.status_code == 409 and one.json()["error"] == "no_show_not_applicable"
    p = await policy(client, world, order)
    assert "no_show" not in p and "relancez" in p["message_fr"]
    # the classic procedure is unchanged: the second alert opens a countdown
    second = await call(client, world.courier_user, "report_no_response", order)
    assert second.status_code == 200 and second.json()["stage"] == "waiting"
    assert [c.messaging_status != "no_show" for c in await the_cases(order)] == [True, True]
    assert await incidents(world.customer) == 0


async def test_last_wait_is_10_min_at_a_prayer_for_every_order(client, world, monkeypatch):
    """Owner 19/10: the 10-minute last wait (prayer / iftar / Ramadan nights) applies to every order in the
    « pas venu » case — here a free-text shop order; the classic alert countdown stays 3 min for it."""
    from app.services import prayer_times

    monkeypatch.setattr(prayer_times, "slow_slot", lambda now, where=prayer_times.SOUSSE: "maghrib")
    order = await on_the_way(world)
    first = await call(client, world.courier_user, "report_no_response", order)
    assert first.json()["timeout_seconds"] == 180  # classic no-response unchanged (D3: partners only)
    await call(client, world.customer, "customer_confirms", order)
    await call(client, world.courier_user, "report_no_response", order)
    await call(client, world.customer, "customer_confirms", order)
    early = await call(client, world.courier_user, "customer_no_show", order)
    assert early.status_code == 409 and 590 <= early.json()["seconds_left"] <= 600
    p = await policy(client, world, order)
    assert p["no_show"]["wait_seconds"] == 600 and "compte à rebours" in p["message_fr"]  # P2: no duration
    await time_passes(order, 300)  # 5 min: not yet
    assert (await call(client, world.courier_user, "customer_no_show", order)).json()["error"] == "wait"
    await time_passes(order, 310)
    assert (await call(client, world.courier_user, "customer_no_show", order)).status_code == 200
