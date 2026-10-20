"""Admin, read only (owner 20/10): listAdminTestAccounts, getAdminCustomerSheet, getAdminCourierSheet.
Admin only — a customer or a courier gets 403, nobody signed out gets 401 — and nothing is written."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    CourierLedgerEntry,
    NoResponseCase,
    Order,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStatusEvent,
    User,
)
from tests.factories import auth
from tests.messaging_factories import make_offer, make_order

FUNCTIONS = ("listAdminTestAccounts", "getAdminCustomerSheet", "getAdminCourierSheet")


async def call(client, name, user=None, payload=None):
    headers = auth(user) if user is not None else {}
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=headers)


@pytest.fixture
async def people(factory):
    admin = await factory.user(email="boss@ods-delivery.tn", role="admin")
    customer = await factory.user(email="amira@gmail.com", full_name="Amira", phone_e164="+21698765432")
    courier_user = await factory.user(email="sami@gmail.com", role="courier", full_name="Sami")
    courier = await factory.courier(
        courier_user, display_name="Sami", verification="verified", is_online=True
    )
    return admin, customer, courier_user, courier


@pytest.mark.parametrize("name", FUNCTIONS)
async def test_admin_only(client, people, name):
    admin, customer, courier_user, courier = people
    payload = {"user_id": str(customer.id), "courier_id": str(courier.id)}
    assert (await call(client, name, None, payload)).status_code == 401
    for caller in (customer, courier_user):
        res = await call(client, name, caller, payload)
        assert res.status_code == 403, (name, caller.email)
        assert "Amira" not in res.text and "+216" not in res.text
    assert (await call(client, name, admin, payload)).status_code == 200


async def test_test_accounts(client, factory, people, monkeypatch):
    admin, customer, courier_user, _ = people
    monkeypatch.setattr(settings, "QA_ACCOUNTS", ["lemelec346@sixoplus.com"])
    listed = await factory.user(email="Lemelec346@sixoplus.com")
    campaign = await factory.user(email="qa-courier-3@example.test", role="courier")
    campaign_courier = await factory.courier(campaign)
    seed = await factory.user(email="pw-seed@ods.tn")
    named = await factory.user(email="someone@yahoo.fr", full_name="QA Client 2")
    real = await factory.user(email="qatar.fan@gmail.com", full_name="Qamar")
    gone = await factory.user(email="deleted+1a2b@deleted.invalid")  # a real person who deleted his account
    body = (await call(client, "listAdminTestAccounts", admin)).json()
    ids = set(body["user_ids"])
    assert {str(listed.id), str(campaign.id), str(seed.id), str(named.id)} <= ids
    assert not ids & {str(real.id), str(gone.id), str(customer.id), str(courier_user.id), str(admin.id)}
    assert body["courier_ids"] == [str(campaign_courier.id)]
    assert {"lemelec346@sixoplus.com", "qa-courier-3@example.test"} <= set(body["emails"])
    assert "qatar.fan@gmail.com" not in body["emails"]


async def test_customer_sheet(client, people):
    admin, customer, courier_user, courier = people
    async with SessionLocal() as s:
        row = await s.get(User, customer.id)
        row.referred_by_courier_id = courier.id
        await s.commit()
    delivered = await make_order(
        customer, courier, status="delivered", delivered_at=datetime.now(UTC),
        purchase_amount=Decimal("12.500"), delivery_fee=Decimal("4.000"),
        delivery_address="Rue de Marseille, Sousse",
    )  # fmt: skip
    cancelled = await make_order(
        customer, status="cancelled", cancelled_by="customer", cancel_reason="changed_mind"
    )
    test = await make_order(customer, items_text="QA TEST 1 pain")
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=delivered.id, status="resolved", started_at=now, deadline_at=now,
                final_at=now, incident_counted=True, purchase_amount=Decimal("12.500"),
            )
        )  # fmt: skip
        s.add(OrderIssue(order_id=delivered.id, reporter_id=courier_user.id, issue_type="wrong_address"))
        await s.commit()
        before = await s.scalar(select(func.count()).select_from(Order))

    res = await call(client, "getAdminCustomerSheet", admin, {"user_id": str(customer.id)})
    assert res.status_code == 200
    body = res.json()
    c = body["customer"]
    assert (c["full_name"], c["phone"], c["email"]) == ("Amira", "+21698765432", "amira@gmail.com")
    assert c["invited_by"] == {"id": str(courier.id), "name": "Sami"} and c["is_test_account"] is False
    assert body["reliability"]["recent_unpaid"] == 1
    assert body["totals"] == {"orders": 3, "delivered": 1, "cancelled": 1}
    by_id = {o["id"]: o for o in body["orders"]}
    d = by_id[str(delivered.id)]
    assert d["courier"] == {"id": str(courier.id), "name": "Sami"}
    assert (d["purchase_amount"], d["delivery_fee"], d["total_amount"]) == (12.5, 4.0, 16.5)
    assert d["delivery_address"] == "Rue de Marseille, Sousse"
    assert (
        by_id[str(cancelled.id)]["cancelled_by"] == "customer" and by_id[str(cancelled.id)]["courier"] is None
    )
    assert by_id[str(test.id)]["is_test_order"] is True and d["is_test_order"] is False
    assert sorted(i["kind"] for i in body["incidents"]) == ["issue", "no_response"]

    assert (await call(client, "getAdminCustomerSheet", admin, {"user_id": "nope"})).status_code == 404
    # by e-mail too: the legacy admin lists name a customer by his e-mail
    by_email = await call(client, "getAdminCustomerSheet", admin, {"user_id": "amira@gmail.com"})
    assert by_email.json()["customer"]["user_id"] == str(customer.id)
    async with SessionLocal() as s:  # read only
        assert await s.scalar(select(func.count()).select_from(Order)) == before


async def test_courier_sheet(client, factory, people):
    admin, customer, courier_user, courier = people
    invited = await factory.user(
        email="invite@gmail.com", full_name="Nour", referred_by_courier_id=courier.id
    )
    delivered = await make_order(
        customer,
        courier,
        status="delivered",
        delivered_at=datetime.now(UTC),
        purchase_amount=Decimal("20"),
        delivery_fee=Decimal("5.000"),
    )
    await make_order(
        invited, courier, status="delivered", delivered_at=datetime.now(UTC), delivery_fee=Decimal("3.000")
    )
    other = await make_order(customer)
    async with SessionLocal() as s:
        accepted = OrderOffer(
            order_id=delivered.id, courier_id=courier.id, proposed_fee=Decimal("5"), status="accepted",
            eta_minutes=25, estimated_eta_minutes=31,
        )  # fmt: skip
        s.add(accepted)
        s.add(
            CourierLedgerEntry(
                courier_id=courier.id, order_id=delivered.id, kind="commission_due", amount=Decimal("0.250")
            )
        )
        s.add(CourierLedgerEntry(courier_id=courier.id, kind="credit_topup", amount=Decimal("-10.000")))
        s.add(OrderRating(order_id=delivered.id, courier_id=courier.id, rater_id=customer.id, rating=4))
        s.add(
            OrderStatusEvent(
                order_id=other.id, from_status="accepted", to_status="pending", source="cancelOrder",
                cancelled_by="courier", actor_user_id=courier_user.id, reason="panne",
            )
        )  # fmt: skip
        await s.commit()
    await make_offer(other, courier, status="pending", fee="6")

    res = await call(client, "getAdminCourierSheet", admin, {"courier_id": str(courier.id)})
    assert res.status_code == 200
    body = res.json()
    c = body["courier"]
    assert c["full_name"] == "Sami" and c["verification_status"] == "verified" and c["is_online"] is True
    assert c["email"] == "sami@gmail.com" and c["average_rating"] == 4.0 and c["ratings_count"] == 1
    assert c["credit_balance"] == pytest.approx(9.75) and c["commission_due_total"] == pytest.approx(0.25)
    row = next(o for o in body["orders"] if o["id"] == str(delivered.id))
    assert row["customer"] == {"id": str(customer.id), "name": "Amira"}
    assert row["commission"] == 0.25 and row["commission_status"] == "due" and row["earnings"] == 4.75
    assert (row["eta_announced"], row["eta_computed"]) == (25, 31)
    assert body["totals"]["delivered"] == 2 and body["totals"]["fees"] == 8.0
    assert {o["status"] for o in body["offers"]} == {"accepted", "pending"}
    assert body["drops"][0]["order_id"] == str(other.id) and body["drops"][0]["reason"] == "panne"
    [inv] = body["invited_clients"]
    assert (inv["user_id"], inv["full_name"], inv["orders"]) == (str(invited.id), "Nour", 1)
    # by the courier's account id too
    for key in (str(courier_user.id), "sami@gmail.com"):
        by_user = await call(client, "getAdminCourierSheet", admin, {"user_id": key})
        assert by_user.json()["courier"]["id"] == str(courier.id)
    assert (
        await call(client, "getAdminCourierSheet", admin, {"user_id": str(customer.id)})
    ).status_code == 404


async def test_owner_test_names_and_contact_aliases_are_test_accounts(client, factory, people):
    # owner 20/10: « Testeur-client », « Test5 » and contact+…@ sign-ups are his own test accounts
    admin, *_ = people
    named = await factory.user(email="someone@hotmail.fr", full_name="Testeur-client")
    numbered = await factory.user(email="x@gmail.com", full_name="Test5")
    alias = await factory.user(email="contact+essai1811@ordering-dashboard-solution.com", full_name="Ali")
    real = await factory.user(email="testa@gmail.com", full_name="Testa Ben Ali")
    res = await call(client, "listAdminTestAccounts", admin, {})
    ids = set(res.json()["user_ids"])
    assert {str(named.id), str(numbered.id), str(alias.id)} <= ids
    assert str(real.id) not in ids
