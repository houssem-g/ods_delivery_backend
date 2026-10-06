"""QA campaign 06/10, wave 4 (team c): incidents, suspension, « Article indisponible ».

R8/R9/R11/R12 the incident rules come from one source — getCustomerReliability returns them
(`rules`), with the date a suspended customer can order again (`suspended_until`, also on
placeOrder's refusal) · R14 an article already reported on an order can't be reported again, even
after the deadline (nor anything after « rien n'est disponible »)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.jobs.orders import stock_check_timeout
from app.models import NoResponseCase
from app.services import orders as order_rules
from app.services.stock_checks import item_key
from tests.order_helpers import OrderWorld
from tests.test_order_flow_functions import order_form
from tests.test_stock_checks import at_shop, call, checks_of, past_deadline, report


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def add_incidents(world, days_ago: list[int]) -> None:
    order = await world.order(status="cancelled")
    async with SessionLocal() as s:
        for days in days_ago:
            when = datetime.now(UTC) - timedelta(days=days)
            s.add(
                NoResponseCase(
                    order_id=order.id, status="resolved", started_at=when, deadline_at=when, final_at=when,
                    incident_counted=True,
                )
            )  # fmt: skip
        await s.commit()


# ─────────────────────────── R8 / R9 / R11 / R12 ───────────────────────────


def test_rules_are_the_server_thresholds():
    rules = order_rules.reliability_from_count(0)["rules"]
    assert rules == {
        "visible_to_couriers_at": 1,  # owner, 2026-09-29: couriers see it from the 1st incident
        "warning_at": 2,
        "limited_at": 3,
        "suspended_at": order_rules.SUSPENDED_AT,
        "limited_max_advance_tnd": 30,
        "window_days": 180,
    }
    assert order_rules.LEVEL_THRESHOLDS["suspended"] == order_rules.SUSPENDED_AT == 5
    assert order_rules.reliability_from_count(1)["visible_to_couriers"] is True


async def test_reliability_returns_the_rules_and_no_end_date_when_not_suspended(client, world):
    await add_incidents(world, [3])
    own = (await call(client, world.customer, "getCustomerReliability")).json()
    assert own["incidents"] == 1 and own["rules"]["visible_to_couriers_at"] == 1
    assert own["suspended"] is False and own["suspended_until"] is None


async def test_suspended_customer_knows_until_when(client, world):
    # 6 incidents: the suspension lasts while 5 stay in the window, i.e. until the 5th most
    # recent (40 days ago) is 180 days old → in 140 days.
    await add_incidents(world, [1, 2, 10, 20, 40, 100])
    own = (await call(client, world.customer, "getCustomerReliability")).json()
    assert own["suspended"] is True and own["incidents"] == 6
    expected = (datetime.now(UTC) + timedelta(days=140)).date().isoformat()
    assert own["suspended_until"].startswith(expected) and own["suspended_until"].endswith("Z")
    order = await world.order()
    seen = (
        await call(client, world.courier_user, "getCustomerReliability", {"order_id": str(order.id)})
    ).json()
    assert seen["suspended"] is True and seen["suspended_until"] is None  # his business only
    admin = (await call(client, world.admin, "getCustomerReliability", {"order_id": str(order.id)})).json()
    assert admin["suspended_until"].startswith(expected)
    refused = await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert refused.status_code == 403
    assert refused.json()["error"] == "customer_suspended"
    assert refused.json()["suspended_until"].startswith(expected)


# ─────────────────────────── R14 ───────────────────────────


def test_item_key_ignores_case_accents_and_quantity():
    assert item_key("2x Lait") == item_key("lait") == item_key("Lait x2") == item_key("2 x LÂIT")
    assert item_key("Article 1") != item_key("Article 2")
    assert item_key("خبز") == "خبز"


async def test_same_article_cannot_be_reported_again_after_the_deadline(client, world):
    order = await at_shop(world)  # call_me: the deadline leaves the check 'expired'
    first = await report(client, world, order, missing_text="Lait")
    assert first.status_code == 200, first.text
    await past_deadline(order)
    await stock_check_timeout()
    [expired] = await checks_of(order)
    assert expired.status == "expired"
    for spelling in ("Lait", "lait", "2x Lait", "LAIT x2"):
        again = await report(client, world, order, missing_text=spelling)
        assert again.status_code == 409, spelling
        assert again.json()["error"] == "item_already_reported"
        assert again.json()["stock_check"]["id"] == first.json()["stock_check"]["id"]
    assert len(await checks_of(order)) == 1  # the customer's phone did not ring again
    other = await report(client, world, order, missing_text="Pain")
    assert other.status_code == 200, other.text  # another article still can


async def test_answered_article_cannot_be_reported_again(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order, missing_text="Coca 1L")).json()["stock_check"]
    r = await call(
        client, world.customer, "answerStockCheck",
        {"order_id": str(order.id), "stock_check_id": check["id"], "decision": "skip"},
    )  # fmt: skip
    assert r.status_code == 200
    again = await report(client, world, order, missing_text="coca 1l")
    assert again.status_code == 409 and again.json()["error"] == "item_already_reported"


@pytest.mark.parametrize("missing", ["Pain", ""])
async def test_nothing_available_then_no_more_reports(client, world, missing):
    order = await at_shop(world)
    assert (await report(client, world, order, missing_text="", nothing_available=True)).status_code == 200
    await past_deadline(order)
    await stock_check_timeout()
    body = {"missing_text": missing, "nothing_available": not missing}
    again = await report(client, world, order, **body)
    assert again.status_code == 409 and again.json()["error"] == "item_already_reported"
