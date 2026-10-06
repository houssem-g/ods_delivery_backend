"""'Article indisponible' (app/services/stock_checks.py): reportUnavailableItems, answerStockCheck,
the timeout job per unavailable_policy, the fault-free cancellation, the courier steps blocked
while the customer decides, the Order document fields and placeOrder's policy."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.jobs.orders import stock_check_timeout
from app.jobs.registry import JOBS
from app.models import (
    Courier,
    File,
    HotDeal,
    Message,
    Order,
    OrderStockCheck,
    PushDelivery,
)
from app.services import push, stock_checks
from app.services.push import PushMessage
from tests.factories import auth
from tests.order_helpers import OrderWorld, device, notifications, reload, rows
from tests.test_order_flow_functions import order_form

BASE = "http://localhost:9110/ods-delivery-test"


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.other_user = await factory.user(email="other-courier@example.test", full_name="Sami", profile=False)
    w.other = await w.make_courier(w.other_user, display_name="Sami")
    w.stranger = await factory.user(email="stranger@example.test")
    return w


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def report(client, world, order: Order, user=None, **body: Any):
    body.setdefault("missing_text", "Coca 1L")
    return await call(
        client, user or world.courier_user, "reportUnavailableItems", {"order_id": str(order.id), **body}
    )


async def answer(client, world, order: Order, check_id: str, decision: str, user=None):
    return await call(
        client,
        user or world.customer,
        "answerStockCheck",
        {"order_id": str(order.id), "stock_check_id": check_id, "decision": decision},
    )


async def at_shop(world, **fields: Any) -> Order:
    fields.setdefault("status", "at_shop")
    return await world.order(courier=world.courier, fee="5", items="Coca 1L, Pain", **fields)


async def checks_of(order: Order) -> list[OrderStockCheck]:
    return await rows(
        select(OrderStockCheck)
        .where(OrderStockCheck.order_id == order.id)
        .order_by(OrderStockCheck.created_at)
    )


async def doc(client, user, order: Order) -> dict[str, Any]:
    return (await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))).json()


async def past_deadline(order: Order, seconds: int = 1) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE order_stock_checks SET deadline_at = now() - make_interval(secs => :s), "
                "created_at = created_at - interval '5 minutes' WHERE order_id = :o"
            ),
            {"s": seconds, "o": order.id},
        )
        await s.commit()


async def late_cancellations(courier: Courier) -> int:
    return (await reload(Courier, courier.id)).late_cancellations


async def chat(order: Order) -> list[Message]:
    return await rows(select(Message).where(Message.order_id == order.id).order_by(Message.created_at))


async def cancel_as_courier(client, world, order: Order, reason: str = "product_unavailable"):
    return await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": reason,
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )


def test_policy_values():
    assert stock_checks.WAIT_SECONDS == 300 and stock_checks.MAX_CHECKS == 5
    assert "stock_check_timeout" in JOBS


def test_stock_check_pushes_ring_and_vibrate():
    for type_ in ("stock_check", "stock_check_answered"):
        msg = PushMessage(type=type_, title_ar="a", title_fr="f", body_ar="", body_fr="", order_id="o-1")
        assert push.build_multicast(["t"], "fr", msg).android.notification.channel_id == "new_orders"
    other = PushMessage(type="at_shop", title_ar="a", title_fr="f", body_ar="", body_fr="", order_id="o-1")
    assert push.build_multicast(["t"], "fr", other).android.notification.channel_id == "default"


# ─────────────────────────── placeOrder / Order document ───────────────────────────


async def test_place_order_keeps_the_policy_and_couriers_see_it(client, world):
    r = await call(
        client,
        world.customer,
        "placeOrder",
        {"order": order_form(unavailable_policy="substitute", alternatives="si pas de Coca, Pepsi")},
    )
    assert r.status_code == 200, r.text
    placed = r.json()["order"]
    assert placed["unavailable_policy"] == "substitute" and placed["alternatives"] == "si pas de Coca, Pepsi"
    # a verified courier bidding on the open order sees the choice, not the stock checks
    seen = (await client.get(f"/api/entities/Order/{placed['id']}", headers=auth(world.other_user))).json()
    assert seen["unavailable_policy"] == "substitute" and seen["stock_check"] is None


async def test_place_order_unknown_policy_is_call_me(client, world):
    for value in ("whatever", None, 3):
        r = await call(client, world.customer, "placeOrder", {"order": order_form(unavailable_policy=value)})
        assert r.status_code == 200 and r.json()["order"]["unavailable_policy"] == "call_me"


# ─────────────────────────── report ───────────────────────────


async def test_report_parks_the_order_and_alerts_the_customer(client, world):
    await device(world.customer)
    order = await at_shop(world)
    r = await report(client, world, order, substitute_text="Pepsi 1L", substitute_price="3.2")
    assert r.status_code == 200, r.text
    body = r.json()
    check = body["stock_check"]
    assert body["order_status"] == "price_confirmation_needed"
    assert check["status"] == "pending" and check["missing_text"] == "Coca 1L"
    assert check["substitute_text"] == "Pepsi 1L" and check["substitute_price"] == 3.2
    assert check["can_accept"] is True and check["can_skip"] is True
    assert check["seconds_left"] in (299, 300) and check["deadline_at"].endswith("Z")
    [row] = await checks_of(order)
    assert row.courier_id == world.courier.id and (row.deadline_at - row.created_at) == timedelta(minutes=5)
    assert row.substitute_price == Decimal("3.200")

    d = await doc(client, world.customer, order)
    assert d["status"] == "price_confirmation_needed" and d["stock_check"]["id"] == check["id"]
    assert d["stock_check"]["status"] == "pending" and len(d["stock_checks"]) == 1
    assert d["status_history"][-1]["source"] == "reportUnavailableItems"

    [alert] = await notifications(world.customer, "stock_check")
    assert (
        alert.title_fr == "🛒 Article indisponible"
        and "Pepsi 1L" in alert.body_fr
        and "3.200 DT" in alert.body_fr
    )
    assert alert.data["stock_check_id"] == check["id"] and alert.data["recipient_role"] == "customer"
    delivered = await rows(select(PushDelivery).where(PushDelivery.notification_id == alert.id))
    assert len(delivered) == 1  # always pushed
    [admin_notice] = await notifications(world.admin, "issue_reported")
    assert admin_notice.data["issue_type"] == "stock_check"
    [line] = await chat(order)
    assert line.sender_role == "courier" and line.recipient_id == world.customer.id and line.is_template
    assert "Coca 1L" in line.body and "Pepsi 1L" in line.body
    # one line per language (French, then Arabic): the app shows the reader's line only (B51)
    fr, ar = line.body.split("\n")
    assert fr.startswith("🛒 Article indisponible") and not re.search("[\u0600-\u06ff]", fr)
    assert re.search("[\u0600-\u06ff]", ar) and "Article" not in ar and "د.ت" in ar


async def test_report_refused_before_arrival_at_shop(client, world):
    """B58: no report (and no « attend au magasin » for the customer) before « Arrivé au magasin »."""
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    r = await report(client, world, order)
    assert r.status_code == 409 and "not_reportable" in r.text, r.text
    assert (await doc(client, world.courier_user, order))["status"] == "accepted"
    lines = await rows(select(Message.body).where(Message.order_id == order.id))
    assert lines == []


async def test_report_nothing_available(client, world):
    order = await at_shop(world)
    r = await report(client, world, order, missing_text="", nothing_available=True, substitute_text="x")
    assert r.status_code == 200, r.text
    check = r.json()["stock_check"]
    assert check["nothing_available"] is True and check["substitute_text"] is None
    assert check["can_accept"] is False and check["can_skip"] is False
    [alert] = await notifications(world.customer, "stock_check")
    assert alert.title_fr == "🛒 Aucun article de votre commande n'est disponible"


async def test_report_with_the_couriers_own_photo(client, world):
    key = "public/issue/2026/09/shelf.jpg"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key,
                owner_id=world.courier_user.id,
                visibility="public",
                content_type="image/jpeg",
                size_bytes=3,
            )
        )
        await s.commit()
    order = await at_shop(world)
    r = await report(client, world, order, photo_url=f"{BASE}/{key}")
    assert r.status_code == 200 and r.json()["stock_check"]["photo_url"] == f"{BASE}/{key}"
    assert (await doc(client, world.customer, order))["stock_check"]["photo_url"] == f"{BASE}/{key}"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"missing_text": "  "}, "missing_text_required"),
        ({"substitute_text": "Pepsi", "substitute_price": "-1"}, "invalid_price"),
        ({"substitute_text": "Pepsi", "substitute_price": "5000"}, "invalid_price"),
        ({"substitute_price": "3"}, "substitute_text_required"),
        ({"photo_url": "https://evil.example/x.jpg"}, "invalid_photo"),
        ({"photo_url": f"{BASE}/public/issue/not-mine.jpg"}, "invalid_photo"),
    ],
)
async def test_report_validation(client, world, body, error):
    order = await at_shop(world)
    r = await report(client, world, order, **body)
    assert r.status_code == 400 and r.json()["error"] == error
    assert await checks_of(order) == []


async def test_only_the_assigned_courier_reports(client, world):
    order = await at_shop(world)
    for user in (world.other_user, world.customer, world.stranger, world.admin):
        r = await report(client, world, order, user=user)
        assert r.status_code == 403, user.email
    r = await report(client, world, order, courier_id=str(world.other.id))
    assert r.status_code == 403
    r = await call(
        client, world.courier_user, "reportUnavailableItems", {"order_id": "nope", "missing_text": "x"}
    )
    assert r.status_code == 404
    assert await checks_of(order) == []


@pytest.mark.parametrize("status", ["purchased", "on_the_way", "delivered", "client_no_response"])
async def test_report_only_before_the_purchase(client, world, status):
    order = await world.order(status=status, courier=world.courier, fee="5", purchase="10")
    r = await report(client, world, order)
    assert r.status_code == 409 and r.json() == {"error": "not_reportable", "status": status}


async def test_report_refused_on_a_hot_deal_order(client, world):
    source = await world.order(status="cancelled", courier=world.courier, fee="5", purchase="10")
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id,
            courier_id=world.courier.id,
            items_text="Lait",
            purchase_amount=Decimal("10"),
            discount_percentage=Decimal("10"),
            price=Decimal("9"),
            status="sold",
            buyer_id=world.customer.id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(deal)
        await s.commit()
        deal_id = deal.id
    order = await at_shop(world, status="accepted", resale_deal_id=deal_id)
    r = await report(client, world, order)
    assert r.status_code == 409 and r.json()["error"] == "not_reportable"


async def test_one_pending_check_at_a_time_and_five_per_order(client, world):
    order = await at_shop(world)
    first = (await report(client, world, order)).json()["stock_check"]
    again = await report(client, world, order, missing_text="Fanta")
    assert again.status_code == 409 and again.json()["error"] == "stock_check_pending"
    assert again.json()["stock_check"]["id"] == first["id"]
    assert (await answer(client, world, order, first["id"], "skip")).status_code == 200
    for n in range(4):
        r = await report(client, world, order, missing_text=f"item {n}")
        assert r.status_code == 200, r.text
        assert (await answer(client, world, order, r.json()["stock_check"]["id"], "skip")).status_code == 200
    r = await report(client, world, order, missing_text="sixth")
    assert r.status_code == 429 and r.json() == {"error": "too_many_stock_checks", "max": 5}


# ─────────────────────────── answer ───────────────────────────


async def test_customer_accepts_the_substitute(client, world):
    await device(world.courier_user)
    order = await at_shop(world)
    check = (await report(client, world, order, substitute_text="Pepsi 1L", substitute_price=3)).json()[
        "stock_check"
    ]
    r = await answer(client, world, order, check["id"], "accept")
    assert r.status_code == 200, r.text
    assert (
        r.json()["order_status"] == "at_shop" and r.json()["stock_check"]["status"] == "substitute_accepted"
    )
    row = (await checks_of(order))[0]
    assert row.decided_by == "customer" and row.decided_at is not None
    [told] = await notifications(world.courier_user, "stock_check_answered")
    assert told.title_fr == "✅ Remplacement accepté" and "Pepsi 1L" in told.body_fr
    assert told.data["decision"] == "substitute_accepted"
    assert len(await rows(select(PushDelivery).where(PushDelivery.notification_id == told.id))) == 1
    lines = await chat(order)
    assert lines[-1].sender_role == "customer" and lines[-1].body.startswith("✅ Remplacement accepté")
    d = await doc(client, world.courier_user, order)
    assert d["status"] == "at_shop" and d["stock_check"]["status"] == "substitute_accepted"
    # the normal next step: purchased with the real amount
    bought = await client.patch(
        f"/api/entities/Order/{order.id}",
        json={"status": "purchased", "purchase_amount": 12.5},
        headers=auth(world.courier_user),
    )
    assert bought.status_code == 200, bought.text
    assert bought.json()["purchase_amount"] == 12.5


async def test_customer_skips_the_item(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order)).json()["stock_check"]
    r = await answer(client, world, order, check["id"], "skip")
    assert r.status_code == 200 and r.json()["order_status"] == "at_shop"
    assert r.json()["stock_check"]["status"] == "item_skipped"
    [told] = await notifications(world.courier_user, "stock_check_answered")
    assert told.title_fr == "➖ Continuer sans l'article"


async def test_customer_cancels_without_any_penalty(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order, nothing_available=True)).json()["stock_check"]
    r = await answer(client, world, order, check["id"], "cancel")
    assert r.status_code == 200 and r.json()["order_status"] == "cancelled"
    row = await reload(Order, order.id)
    assert row.cancelled_by == "customer" and row.cancel_reason == "product_unavailable"
    assert row.courier_id == world.courier.id  # the courier still sees the cancelled order
    assert await late_cancellations(world.courier) == 0
    [told] = await notifications(world.courier_user, "stock_check_answered")
    assert told.title_fr == "❌ Commande annulée" and "Aucune pénalité" in told.body_fr
    d = await doc(client, world.customer, order)
    assert d["status"] == "cancelled" and d["cancellation_reason"] == "product_unavailable"
    assert d["stock_check"]["status"] == "order_cancelled"


async def test_only_the_customer_answers(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order, substitute_text="Pepsi")).json()["stock_check"]
    for user in (world.courier_user, world.other_user, world.stranger, world.admin):
        r = await answer(client, world, order, check["id"], "accept", user=user)
        assert r.status_code == 403, user.email
    assert (await checks_of(order))[0].status == "pending"


async def test_answer_refusals(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order)).json()["stock_check"]
    r = await answer(client, world, order, check["id"], "maybe")
    assert r.status_code == 400 and r.json()["error"] == "invalid_decision"
    r = await call(client, world.customer, "answerStockCheck", {"order_id": str(order.id)})
    assert r.status_code == 400
    r = await answer(client, world, order, "00000000-0000-0000-0000-000000000000", "skip")
    assert r.status_code == 404 and r.json()["error"] == "stock_check_not_found"
    r = await answer(client, world, order, check["id"], "accept")  # no substitute proposed
    assert r.status_code == 409 and r.json()["error"] == "accept_not_allowed"
    assert (await answer(client, world, order, check["id"], "skip")).status_code == 200
    r = await answer(client, world, order, check["id"], "cancel")
    assert r.status_code == 409 and r.json()["error"] == "already_decided"
    assert r.json()["order_status"] == "at_shop"
    assert (await reload(Order, order.id)).status == "at_shop"


async def test_nothing_available_can_only_be_cancelled(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order, nothing_available=True)).json()["stock_check"]
    r = await answer(client, world, order, check["id"], "accept")
    assert r.status_code == 409 and r.json()["error"] == "accept_not_allowed"
    r = await answer(client, world, order, check["id"], "skip")
    assert r.status_code == 409 and r.json()["error"] == "skip_not_allowed"


async def test_courier_steps_wait_for_the_customer(client, world):
    order = await at_shop(world)
    check = (await report(client, world, order)).json()["stock_check"]
    for status in ("purchased", "at_shop"):
        r = await client.patch(
            f"/api/entities/Order/{order.id}",
            json={"status": status, "purchase_amount": 5},
            headers=auth(world.courier_user),
        )
        assert r.status_code == 409 and r.json()["error"] == "stock_check_pending", status
    await answer(client, world, order, check["id"], "skip")
    r = await client.patch(
        f"/api/entities/Order/{order.id}",
        json={"status": "purchased", "purchase_amount": 5},
        headers=auth(world.courier_user),
    )
    assert r.status_code == 200, r.text


async def test_an_answer_just_past_the_deadline_still_wins(client, world):
    order = await at_shop(world, unavailable_policy="cancel")
    check = (await report(client, world, order, substitute_text="Pepsi")).json()["stock_check"]
    await past_deadline(order)
    r = await answer(client, world, order, check["id"], "accept")
    assert r.status_code == 200 and r.json()["order_status"] == "at_shop"
    assert (await stock_check_timeout())["advanced"] == 0


# ─────────────────────────── timeout ───────────────────────────


@pytest.mark.parametrize(
    ("policy", "substitute", "nothing", "check_status", "order_status"),
    [
        ("substitute", "Pepsi", False, "substitute_accepted", "at_shop"),
        ("substitute", None, False, "item_skipped", "at_shop"),
        ("skip", "Pepsi", False, "item_skipped", "at_shop"),
        ("cancel", None, False, "order_cancelled", "cancelled"),
        ("call_me", "Pepsi", False, "expired", "price_confirmation_needed"),
        ("skip", None, True, "expired", "price_confirmation_needed"),
        ("substitute", None, True, "expired", "price_confirmation_needed"),
        ("cancel", None, True, "order_cancelled", "cancelled"),
    ],
)
async def test_timeout_applies_the_policy(
    client, world, policy, substitute, nothing, check_status, order_status
):
    order = await at_shop(world, unavailable_policy=policy)
    body: dict[str, Any] = {"nothing_available": nothing}
    if substitute:
        body["substitute_text"] = substitute
    assert (await report(client, world, order, **body)).status_code == 200
    assert (await stock_check_timeout())["checked"] == 0  # not due yet
    await past_deadline(order)
    result = await stock_check_timeout()
    assert result == {"checked": 1, "advanced": 1, "errors": 0}
    [row] = await checks_of(order)
    assert row.status == check_status and row.decided_by == "system"
    fresh = await reload(Order, order.id)
    assert fresh.status == order_status
    if order_status == "cancelled":
        assert fresh.cancelled_by == "system" and fresh.cancel_reason == "product_unavailable"
    [courier_notice] = await notifications(world.courier_user, "stock_check_answered")
    assert courier_notice.data["decided_by"] == "system"
    if check_status == "expired":
        assert "appelez le client ou annulez sans pénalité" in courier_notice.body_fr
        assert courier_notice.data["can_cancel_without_penalty"] is True
    customer_notices = await notifications(world.customer, "stock_check")
    assert len(customer_notices) == 2 and customer_notices[-1].data["decided_by"] == "system"
    assert (await stock_check_timeout())["checked"] == 0  # idempotent
    assert await late_cancellations(world.courier) == 0


async def test_late_answer_after_call_me_expiry(client, world):
    order = await at_shop(world)  # call_me by default
    check = (await report(client, world, order, substitute_text="Pepsi")).json()["stock_check"]
    await past_deadline(order)
    await stock_check_timeout()
    r = await answer(client, world, order, check["id"], "accept")
    assert r.status_code == 200 and r.json()["order_status"] == "at_shop"
    assert r.json()["stock_check"]["decided_by"] == "customer"


# ─────────────────────────── courier cancellation ───────────────────────────


async def test_courier_cancels_without_penalty_after_an_unanswered_check(client, world):
    order = await at_shop(world)
    await report(client, world, order)
    await past_deadline(order)
    await stock_check_timeout()
    r = await cancel_as_courier(client, world, order)
    assert r.status_code == 200, r.text
    fresh = await reload(Order, order.id)
    assert fresh.status == "cancelled" and fresh.cancelled_by == "courier"  # not back to the pool
    assert fresh.courier_id == world.courier.id
    assert await late_cancellations(world.courier) == 0
    [told] = (await notifications(world.customer, "order_cancelled"))[-1:]
    assert told.data["fault_free"] is True and "Sans frais" in told.body_fr


async def test_courier_cancel_right_after_the_deadline_resolves_it_first(client, world):
    order = await at_shop(world)
    await report(client, world, order)
    await past_deadline(order)  # the job has not run yet
    r = await cancel_as_courier(client, world, order)
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).status == "cancelled"
    assert (await checks_of(order))[0].status == "expired"
    assert await late_cancellations(world.courier) == 0


async def test_courier_cancel_when_the_policy_already_cancels(client, world):
    order = await at_shop(world, unavailable_policy="cancel")
    await report(client, world, order)
    await past_deadline(order)
    r = await cancel_as_courier(client, world, order)
    assert r.status_code == 200, r.text
    fresh = await reload(Order, order.id)
    assert fresh.status == "cancelled" and fresh.cancelled_by == "system"
    assert await late_cancellations(world.courier) == 0


async def test_product_unavailable_without_proof_is_a_late_cancellation(client, world):
    # no stock check at all
    order = await at_shop(world)
    assert (await cancel_as_courier(client, world, order)).status_code == 200
    assert (await reload(Order, order.id)).status == "pending"  # back to the pool
    assert await late_cancellations(world.courier) == 1


async def test_cancel_while_the_customer_decides_is_not_fault_free(client, world):
    order = await at_shop(world)
    await report(client, world, order)
    assert (await cancel_as_courier(client, world, order)).status_code == 200
    fresh = await reload(Order, order.id)
    assert fresh.status == "pending" and fresh.courier_id is None
    [row] = await checks_of(order)
    assert row.status == "expired" and row.decided_by == "system"  # closed with the drop
    assert (await doc(client, world.customer, order))["stock_check"]["status"] == "expired"


async def test_another_couriers_expired_check_proves_nothing(client, world):
    order = await at_shop(world)
    await report(client, world, order)
    await past_deadline(order)
    await stock_check_timeout()
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE order_stock_checks SET courier_id = :c WHERE order_id = :o"),
            {"c": world.other.id, "o": order.id},
        )
        await s.commit()
    assert (await cancel_as_courier(client, world, order)).status_code == 200
    assert (await reload(Order, order.id)).status == "pending"


async def test_stock_checks_hidden_from_non_parties(client, world):
    order = await at_shop(world)
    await report(client, world, order)
    admin_doc = await doc(client, world.admin, order)
    assert admin_doc["stock_check"]["status"] == "pending" and len(admin_doc["stock_checks"]) == 1
    r = await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.stranger))
    assert r.status_code == 404
