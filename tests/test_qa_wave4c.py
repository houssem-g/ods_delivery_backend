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
        "limited_max_advance_tnd": 30,
        "window_days": 180,
    }
    assert "suspended" not in order_rules.LEVEL_THRESHOLDS  # owner, 10/10/2026
    assert order_rules.reliability_from_count(1)["visible_to_couriers"] is True


async def test_reliability_returns_the_rules_and_no_end_date_when_not_suspended(client, world):
    await add_incidents(world, [3])
    own = (await call(client, world.customer, "getCustomerReliability")).json()
    assert own["incidents"] == 1 and own["rules"]["visible_to_couriers_at"] == 1
    assert own["suspended"] is False and own["suspended_until"] is None


async def test_many_incidents_never_suspend(client, world):
    # Owner, 10/10/2026: 6 incidents, still free to order; couriers see the badge instead.
    await add_incidents(world, [1, 2, 10, 20, 40, 100])
    own = (await call(client, world.customer, "getCustomerReliability")).json()
    assert own["suspended"] is False and own["suspended_until"] is None and own["incidents"] == 6
    assert own["level"] == "limited"
    placed = await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert placed.status_code == 200


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
