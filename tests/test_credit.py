"""Courier prepaid credit (decision D-10): getMyCredit, the credit_empty refusal of createOrderOffer,
bank-counter top-ups (requestCreditTopup / reviewCreditTopup / listCreditTopups), the cashier
(setCreditCashier / lookupCourierForTopup / cashierCreditTopup / listMyCashierTopups /
markCashierRemitted), primes and corrections (grantCourierCredit), settings and the low-credit alert."""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CourierLedgerEntry, CreditTopup, File
from app.services import commission, credit
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, now


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
def enforced(monkeypatch):
    """The credit rules apply (after 1 January 2027)."""
    monkeypatch.setattr(credit, "CREDIT_ENFORCED_FROM", now() - timedelta(days=1))


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def ledger(courier, kind: str, amount: str, order_id=None) -> None:
    async with SessionLocal() as s:
        s.add(CourierLedgerEntry(courier_id=courier.id, kind=kind, amount=Decimal(amount), order_id=order_id))
        await s.commit()


async def deliveries(world, n: int, kind: str = "commission_waived_quota") -> None:
    """n deliveries of this month, each with its commission entry."""
    for _ in range(n):
        order = await world.order(status="delivered", courier=world.courier, fee="5")
        await ledger(world.courier, kind, "0.500", order.id)


async def receipt(owner, *, content_type="image/jpeg") -> str:
    key = f"private/generic/{owner.id}/{uuid.uuid4().hex}.jpg"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key,
                owner_id=owner.id,
                visibility="private",
                purpose="generic",
                content_type=content_type,
                size_bytes=10,
            )
        )
        await s.commit()
    return key


async def test_summary_before_the_launch_ends_shows_what_he_would_have_paid(client, world):
    await deliveries(world, 22, kind="commission_waived_launch")
    r = await call(client, world.courier_user, "getMyCredit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enforced"] is False and body["blocked"] is False
    assert body["credit"] == "0.000" and body["month_deliveries"] == 22 and body["free_left"] == 0
    assert body["would_have_paid"] == "0.500"  # 2 deliveries beyond the 20 free ones, 0.250 each
    assert [t["bonus"] for t in body["suggested_topups"]] == ["0.000", "0.000"]
    assert (await call(client, world.customer, "getMyCredit")).json()["error"] == "courier_profile_missing"


async def test_offer_refused_when_free_deliveries_used_and_credit_empty(client, world, enforced):
    order = await world.order()
    await deliveries(world, 19)
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 200, r.text  # one free delivery left this month

    await deliveries(world, 1)
    other = await world.order()
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(other.id), "fee": 5})
    assert r.status_code == 402 and r.json()["error"] == "credit_empty", r.text
    assert r.json()["credit"] == "0.000"
    summary = (await call(client, world.courier_user, "getMyCredit")).json()
    assert summary["blocked"] is True and summary["low"] is True

    await ledger(world.courier, "credit_topup", "-0.500")
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(other.id), "fee": 5})
    assert r.status_code == 200, r.text


async def test_offer_never_refused_before_enforcement(client, world):
    await deliveries(world, 25)
    order = await world.order()
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 200, r.text


async def test_bank_deposit_topup_reviewed_by_an_admin(client, world):
    key = await receipt(world.courier_user)
    bad = await call(client, world.courier_user, "requestCreditTopup", {"amount": 3, "receipt_url": key})
    assert bad.json()["error"] == "invalid_amount"
    bad = await call(client, world.courier_user, "requestCreditTopup", {"amount": 10.5, "receipt_url": key})
    assert bad.json()["error"] == "invalid_amount"
    other = await receipt(world.customer)
    bad = await call(client, world.courier_user, "requestCreditTopup", {"amount": 10, "receipt_url": other})
    assert bad.json()["error"] == "invalid_receipt"

    r = await call(
        client,
        world.courier_user,
        "requestCreditTopup",
        {"amount": 10, "receipt_url": key, "reference": " 12 34 "},
    )
    assert r.status_code == 200, r.text
    topup = r.json()["topup"]
    assert topup["status"] == "pending" and topup["amount"] == "10.000" and topup["reference"] == "12 34"
    async with SessionLocal() as s:
        assert (await s.execute(select(File.purpose).where(File.key == key))).scalar_one() == "credit_receipt"
    assert [n.type for n in await notifications(world.admin)] == ["credit_topup_pending"]
    again = await call(client, world.courier_user, "requestCreditTopup", {"amount": 10, "receipt_url": key})
    assert again.status_code == 409 and again.json()["error"] == "receipt_already_used"
    pending = (await call(client, world.courier_user, "getMyCredit")).json()
    assert pending["credit"] == "0.000" and len(pending["pending_topups"]) == 1

    assert (
        await call(client, world.courier_user, "reviewCreditTopup", {"id": topup["id"], "status": "approved"})
    ).status_code == 403
    listed = (await call(client, world.admin, "listCreditTopups", {"status": "pending"})).json()["topups"]
    assert [t["id"] for t in listed] == [topup["id"]] and listed[0]["receipt_url"]
    assert listed[0]["courier_name"] == "Karim Trabelsi"

    r = await call(client, world.admin, "reviewCreditTopup", {"id": topup["id"], "status": "approved"})
    assert r.status_code == 200, r.text
    assert r.json()["topup"]["bonus"] == "0.000"
    after = (await call(client, world.courier_user, "getMyCredit")).json()
    assert after["credit"] == "10.000" and after["deliveries_covered"] == 40 and after["pending_topups"] == []
    assert {h["kind"]: h["amount"] for h in after["history"]} == {"credit_topup": "10.000"}
    [told] = await notifications(world.courier_user, "credit_topup_approved")
    assert "10.000 DT" in told.body_fr and "bonus" not in told.body_fr
    twice = await call(client, world.admin, "reviewCreditTopup", {"id": topup["id"], "status": "rejected"})
    assert twice.status_code == 409 and twice.json()["error"] == "topup_already_reviewed"


async def test_admin_may_correct_the_amount_or_reject(client, world):
    first = (
        await call(
            client,
            world.courier_user,
            "requestCreditTopup",
            {"amount": 20, "receipt_url": await receipt(world.courier_user)},
        )
    ).json()["topup"]
    r = await call(
        client, world.admin, "reviewCreditTopup", {"id": first["id"], "status": "approved", "amount": 15}
    )
    assert r.json()["topup"]["amount"] == "15.000" and r.json()["topup"]["bonus"] == "0.000"
    second = (
        await call(
            client,
            world.courier_user,
            "requestCreditTopup",
            {"amount": 10, "receipt_url": await receipt(world.courier_user)},
        )
    ).json()["topup"]
    r = await call(
        client,
        world.admin,
        "reviewCreditTopup",
        {"id": second["id"], "status": "rejected", "note": "Reçu illisible"},
    )
    assert r.json()["topup"]["status"] == "rejected"
    [told] = await notifications(world.courier_user, "credit_topup_rejected")
    assert "Reçu illisible" in told.body_fr
    assert (await call(client, world.courier_user, "getMyCredit")).json()["credit"] == "15.000"


async def test_too_many_pending_topups(client, world):
    for _ in range(credit.MAX_PENDING_TOPUPS):
        r = await call(
            client,
            world.courier_user,
            "requestCreditTopup",
            {"amount": 5, "receipt_url": await receipt(world.courier_user)},
        )
        assert r.status_code == 200, r.text
    r = await call(
        client,
        world.courier_user,
        "requestCreditTopup",
        {"amount": 5, "receipt_url": await receipt(world.courier_user)},
    )
    assert r.status_code == 429 and r.json()["error"] == "too_many_pending_topups"


async def test_cashier_sells_credit_for_cash(client, world, factory):
    cafe = await factory.user(email="cafe@example.test", full_name="Café ODS")
    assert (await call(client, cafe, "lookupCourierForTopup", {"phone": "55123456"})).json()[
        "error"
    ] == "not_a_cashier"
    assert (
        await call(
            client, world.courier_user, "setCreditCashier", {"email": "cafe@example.test", "label": "X"}
        )
    ).status_code == 403
    r = await call(
        client,
        world.admin,
        "setCreditCashier",
        {"email": "CAFE@example.test", "label": "Café ODS Sousse", "address": "Khezama"},
    )
    assert r.status_code == 200, r.text

    who = await call(client, cafe, "lookupCourierForTopup", {"phone": "55 123 456"})
    assert who.json()["courier"] == {"name": "Karim T.", "phone_end": "56", "verified": True}
    assert (await call(client, cafe, "lookupCourierForTopup", {"phone": "99999999"})).json()[
        "error"
    ] == "courier_not_found"

    r = await call(client, cafe, "cashierCreditTopup", {"phone": "+21655123456", "amount": 20})
    assert r.status_code == 200, r.text
    assert r.json()["credited"] == "20.000" and r.json()["cash_to_collect"] == "20.000"
    assert (await call(client, world.courier_user, "getMyCredit")).json()["credit"] == "20.000"
    assert await notifications(world.courier_user, "credit_topup_approved")

    drawer = (await call(client, cafe, "listMyCashierTopups")).json()
    assert drawer["to_hand_over"] == "20.000" and len(drawer["topups"]) == 1
    [row] = (await call(client, world.admin, "listCreditCashiers")).json()["cashiers"]
    assert row["to_hand_over"] == "20.000" and row["label"] == "Café ODS Sousse"

    done = await call(client, world.admin, "markCashierRemitted", {"cashier_user_id": row["user_id"]})
    assert done.json() == {"success": True, "sales": 1, "amount": "20.000"}
    assert (await call(client, cafe, "listMyCashierTopups")).json()["to_hand_over"] == "0.000"

    await call(client, world.admin, "setCreditCashier", {"email": "cafe@example.test", "active": False})
    gone = await call(client, cafe, "cashierCreditTopup", {"phone": "+21655123456", "amount": 10})
    assert gone.status_code == 403


async def test_cashier_cannot_credit_himself(client, world):
    await call(client, world.admin, "setCreditCashier", {"email": "courier@example.test", "label": "Moi"})
    r = await call(client, world.courier_user, "cashierCreditTopup", {"phone": "+21655123456", "amount": 10})
    assert r.status_code == 403 and r.json()["error"] == "own_credit"


async def test_primes_and_corrections(client, world):
    payload = {
        "courier_id": str(world.courier.id),
        "kind": "prime",
        "amount": 30,
        "reason": "Prime fondateur",
    }
    assert (await call(client, world.courier_user, "grantCourierCredit", payload)).status_code == 403
    r = await call(client, world.admin, "grantCourierCredit", payload)
    assert r.status_code == 200 and r.json()["credit"] == "30.000", r.text
    [prime] = await notifications(world.courier_user, "credit_topup_approved")
    assert "Prime fondateur" in prime.body_fr
    r = await call(
        client,
        world.admin,
        "grantCourierCredit",
        {"courier_id": str(world.courier.id), "kind": "adjustment", "amount": -2.5, "reason": "Erreur"},
    )
    assert r.json()["credit"] == "27.500"
    bad = await call(client, world.admin, "grantCourierCredit", {**payload, "reason": ""})
    assert bad.json()["error"] == "invalid_reason"
    bad = await call(client, world.admin, "grantCourierCredit", {**payload, "amount": 500})
    assert bad.json()["error"] == "invalid_amount"


async def test_settings_are_shown_on_my_credit(client, world, factory):
    assert (await call(client, world.courier_user, "setCreditSettings", {"rib": "x"})).status_code == 403
    r = await call(
        client,
        world.admin,
        "setCreditSettings",
        {"bank_name": "Attijari bank", "account_holder": "ODS", "rib": "04 000 0000000000000 00"},
    )
    assert r.status_code == 200, r.text
    cafe = await factory.user(email="cafe@example.test", full_name="Café")
    await call(
        client,
        world.admin,
        "setCreditCashier",
        {"email": cafe.email, "label": "Café ODS", "address": "Sousse"},
    )
    assert (await call(client, world.courier_user, "getCreditSettings")).status_code == 403
    saved = (await call(client, world.admin, "getCreditSettings")).json()["settings"]
    assert saved["bank_name"] == "Attijari bank" and saved["instructions_fr"] is None
    where = (await call(client, world.courier_user, "getMyCredit")).json()["where"]
    assert where["bank"]["bank_name"] == "Attijari bank" and where["bank"]["rib"].startswith("04")
    assert where["cashiers"] == [{"label": "Café ODS", "address": "Sousse"}]


async def test_low_credit_alert_once_when_crossing(world, monkeypatch):
    monkeypatch.setattr(commission, "LAUNCH_FREE", False)
    monkeypatch.setattr(commission, "FREE_DELIVERIES_PER_MONTH", 0)
    await ledger(world.courier, "credit_topup", "-1.000")  # 4 commissions of 0.250
    for expected in (0, 1, 1, 1):  # 0.750 (not under 3 commissions yet), 0.500 (crossed), 0.250, 0
        order = await world.order(status="delivered", courier=world.courier, fee="5")
        async with SessionLocal() as s:
            row = await s.get(type(order), order.id)
            entry = await commission.record_delivery(s, row, now())
            await s.commit()
        assert entry is not None and entry.kind == "commission_due"
        assert len(await notifications(world.courier_user, "credit_low")) == expected
    async with SessionLocal() as s:
        assert await credit.balance(s, world.courier.id) == Decimal("0")
        due = (
            await s.execute(select(CourierLedgerEntry).where(CourierLedgerEntry.kind == "commission_due"))
        ).scalars()
        assert len(list(due)) == 4
    async with SessionLocal() as s:
        assert (await s.execute(select(CreditTopup))).scalars().first() is None
