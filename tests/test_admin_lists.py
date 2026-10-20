"""Admin, read only (owner 20/10): listAdminOrders, listAdminCouriers, listAdminUsers — the filtered,
sorted, paginated tabs. Admin only (403 / 401), « Masquer les tests » on by default, nothing written."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import CourierLedgerEntry, NoResponseCase, Order, OrderRating, User
from tests.factories import auth
from tests.messaging_factories import make_offer, make_order

FUNCTIONS = ("listAdminOrders", "listAdminCouriers", "listAdminUsers")


async def call(client, name, user=None, payload=None):
    headers = auth(user) if user is not None else {}
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=headers)


@pytest.fixture
async def world(factory):
    admin = await factory.user(email="boss@ods-delivery.tn", role="admin", full_name="Boss")
    amira = await factory.user(email="amira@gmail.com", full_name="Amira", phone_e164="+21698765432")
    nour = await factory.user(email="nour@gmail.com", full_name="Nour", google_sub="g-123")
    qa = await factory.user(email="qa-client-1@example.test", full_name="Client QA")
    sami_user = await factory.user(email="sami@gmail.com", role="courier", full_name="Sami")
    sami = await factory.courier(
        sami_user, display_name="Sami", verification="verified", is_online=True, vehicle_plate="123 TU 4567",
        last_seen_at=datetime.now(UTC),
    )  # fmt: skip
    ali_user = await factory.user(email="ali@gmail.com", role="courier", full_name="Ali")
    ali = await factory.courier(ali_user, display_name="Ali", vehicle="car", late_cancellations=2)
    qa_k_user = await factory.user(email="qa-courier-1@example.test", role="courier", full_name="Livreur QA")
    qa_k = await factory.courier(qa_k_user, display_name="Livreur QA")
    async with SessionLocal() as s:
        (await s.get(User, nour.id)).referred_by_courier_id = sami.id
        await s.commit()

    now = datetime.now(UTC)
    delivered = await make_order(
        amira, sami, status="delivered", delivered_at=now, purchase_amount=Decimal("12.500"),
        delivery_fee=Decimal("4.000"), items_text="2x Baguette\n1x Lait Délice", notes="Sonnez deux fois",
        delivery_address="Rue de Marseille, Sousse",
    )  # fmt: skip
    pending = await make_order(nour, items_text="Doliprane 1000", estimated_price=Decimal("8"))
    await make_offer(pending, sami)
    await make_offer(pending, ali)
    cancelled = await make_order(
        amira, status="cancelled", cancelled_by="customer", cancel_reason="changed_mind", items_text="Pizza"
    )
    test_text = await make_order(amira, items_text="QA TEST pain")
    test_customer = await make_order(qa, items_text="Lait")
    test_courier = await make_order(nour, qa_k, status="accepted", items_text="Eau 1,5 L")
    async with SessionLocal() as s:
        s.add(
            NoResponseCase(
                order_id=delivered.id, status="resolved", started_at=now, deadline_at=now,
                final_at=now, incident_counted=True, purchase_amount=Decimal("12.500"),
            )
        )  # fmt: skip
        s.add(
            CourierLedgerEntry(
                courier_id=sami.id, order_id=delivered.id, kind="commission_due", amount=Decimal("0.250")
            )
        )
        s.add(OrderRating(order_id=delivered.id, courier_id=sami.id, rater_id=amira.id, rating=5))
        old = await s.get(Order, cancelled.id)
        old.created_at = now - timedelta(days=10)
        await s.commit()
    return {
        "admin": admin, "amira": amira, "nour": nour, "qa": qa, "sami": sami, "ali": ali, "qa_k": qa_k,
        "sami_user": sami_user, "delivered": delivered, "pending": pending, "cancelled": cancelled,
        "test_text": test_text, "test_customer": test_customer, "test_courier": test_courier,
    }  # fmt: skip


@pytest.mark.parametrize("name", FUNCTIONS)
async def test_admin_only(client, world, name):
    assert (await call(client, name)).status_code == 401
    for caller in (world["amira"], world["sami_user"]):
        res = await call(client, name, caller)
        assert res.status_code == 403
        assert "Amira" not in res.text and "+216" not in res.text
    assert (await call(client, name, world["admin"])).status_code == 200


async def ids(client, world, payload):
    body = (await call(client, "listAdminOrders", world["admin"], payload)).json()
    return {o["id"] for o in body["orders"]}, body


async def test_orders_hide_tests_and_details(client, world):
    async with SessionLocal() as s:
        before = await s.scalar(select(func.count()).select_from(Order))
    shown, body = await ids(client, world, {})
    real = {str(world[k].id) for k in ("delivered", "pending", "cancelled")}
    assert shown == real
    assert body["total"] == 3
    expected = {"orders": 3, "delivered": 1, "items": 12.5, "fees": 4.0}
    assert body["totals"] == {**expected, "commission_due": 0.25, "commission_offered": 0.0}
    every, body_all = await ids(client, world, {"hide_tests": False})
    assert len(every) == 6
    flags = {o["id"]: o["is_test_order"] for o in body_all["orders"]}
    assert flags[str(world["test_courier"].id)] and flags[str(world["test_customer"].id)]
    assert flags[str(world["test_text"].id)] and not flags[str(world["delivered"].id)]

    d = next(o for o in body["orders"] if o["id"] == str(world["delivered"].id))
    assert d["items_text"] == "2x Baguette\n1x Lait Délice" and d["notes"] == "Sonnez deux fois"
    assert d["stops"][0]["name"] == "Carrefour" and d["type"] == "free"
    assert d["customer"]["name"] == "Amira" and d["customer"]["phone"] == "+21698765432"
    assert d["courier"]["name"] == "Sami"
    assert (d["items_amount"], d["delivery_fee"], d["total_amount"]) == (12.5, 4.0, 16.5)
    assert (d["commission"], d["commission_status"], d["payment_method"]) == (0.25, "due", "cash")
    assert d["incidents"][0]["kind"] == "no_response" and d["incidents"][0]["counted"] is True
    p = next(o for o in body["orders"] if o["id"] == str(world["pending"].id))
    assert p["offers_count"] == 2 and p["items_amount"] == 8.0 and p["courier"] is None
    async with SessionLocal() as s:  # read only
        assert await s.scalar(select(func.count()).select_from(Order)) == before


async def test_orders_filters(client, world):
    d, p, c = (str(world[k].id) for k in ("delivered", "pending", "cancelled"))
    assert (await ids(client, world, {"status": ["delivered"]}))[0] == {d}
    assert (await ids(client, world, {"status": ["waiting", "offers"]}))[0] == {p}
    assert (await ids(client, world, {"status": "cancelled"}))[0] == {c}
    assert (await ids(client, world, {"period": "7d"}))[0] == {d, p}
    today = datetime.now(UTC).date()
    assert (
        await ids(
            client,
            world,
            {"date_from": str(today - timedelta(days=12)), "date_to": str(today - timedelta(days=8))},
        )
    )[0] == {c}
    assert (await ids(client, world, {"type": "free"}))[0] == {d, p, c}
    assert (await ids(client, world, {"type": "vitrine"}))[0] == set()
    assert (await ids(client, world, {"customer": "amira"}))[0] == {d, c}
    assert (await ids(client, world, {"customer": "98765"}))[0] == {d, c}
    assert (await ids(client, world, {"courier": "sami"}))[0] == {d}
    assert (await ids(client, world, {"shop": "carref"}))[0] == {d, p, c}
    assert (await ids(client, world, {"amount_min": 10}))[0] == {d}
    assert (await ids(client, world, {"amount_max": "9,5"}))[0] == {p}
    assert (await ids(client, world, {"incident": "yes"}))[0] == {d}
    assert (await ids(client, world, {"incident": "no"}))[0] == {p, c}
    assert (await ids(client, world, {"q": "baguette"}))[0] == {d}
    assert (await ids(client, world, {"q": "marseille"}))[0] == {d}
    assert (await ids(client, world, {"q": d[:8]}))[0] == {d}
    assert (await ids(client, world, {"q": "100%_"}))[0] == set()  # wildcards are plain text
    first = (await call(client, "listAdminOrders", world["admin"], {"sort": "amount_desc"})).json()["orders"]
    assert first[0]["id"] == d
    paged = (await call(client, "listAdminOrders", world["admin"], {"page": 2, "page_size": 2})).json()
    assert len(paged["orders"]) == 1 and paged["total"] == 3 and paged["page"] == 2
    huge = (await call(client, "listAdminOrders", world["admin"], {"page_size": 5000})).json()
    assert huge["page_size"] == 100


async def test_couriers_list(client, world):
    body = (await call(client, "listAdminCouriers", world["admin"])).json()
    names = [c["full_name"] for c in body["couriers"]]
    assert sorted(names) == ["Ali", "Sami"] and body["totals"] == {"couriers": 2, "online": 1, "pending": 1}
    sami = next(c for c in body["couriers"] if c["full_name"] == "Sami")
    assert (sami["deliveries"], sami["deliveries_30d"], sami["fees"], sami["earnings"]) == (1, 1, 4.0, 3.75)
    assert (sami["average_rating"], sami["ratings_count"], sami["invited_clients"]) == (5.0, 1, 1)
    assert sami["commission_due_total"] == 0.25 and sami["credit_balance"] == -0.25
    assert sami["last_seen_at"] and sami["is_online"] is True

    async def names_for(payload):
        res = (await call(client, "listAdminCouriers", world["admin"], payload)).json()
        return [c["full_name"] for c in res["couriers"]]

    assert sorted(await names_for({"hide_tests": False})) == ["Ali", "Livreur QA", "Sami"]
    assert await names_for({"verification": "verified"}) == ["Sami"]
    assert await names_for({"online": "yes"}) == ["Sami"]
    assert await names_for({"vehicle": "car"}) == ["Ali"]
    assert await names_for({"late_cancellations": True}) == ["Ali"]
    assert await names_for({"active": "7d"}) == ["Sami"]
    assert await names_for({"q": "123TU"}) == ["Sami"]
    assert await names_for({"q": "ali@gmail"}) == ["Ali"]
    assert await names_for({"period": "today"}) != []
    assert (await names_for({"sort": "deliveries"}))[0] == "Sami"


async def test_users_list(client, world):
    body = (await call(client, "listAdminUsers", world["admin"])).json()
    by_email = {u["email"]: u for u in body["users"]}
    assert "qa-client-1@example.test" not in by_email and "qa-courier-1@example.test" not in by_email
    amira = by_email["amira@gmail.com"]
    assert (amira["orders"], amira["delivered"], amira["spent"], amira["unpaid"]) == (3, 1, 16.5, 1)
    assert amira["signup"] == "email" and amira["role"] == "customer" and amira["last_activity"]
    nour = by_email["nour@gmail.com"]
    assert nour["signup"] == "google" and nour["invited_by"] == {"id": str(world["sami"].id), "name": "Sami"}

    async def emails(payload):
        res = (await call(client, "listAdminUsers", world["admin"], payload)).json()
        return {u["email"] for u in res["users"]}

    assert await emails({"role": "courier"}) == {"sami@gmail.com", "ali@gmail.com"}
    assert await emails({"role": "admin"}) == {"boss@ods-delivery.tn"}
    assert await emails({"has_orders": "yes"}) == {"amira@gmail.com", "nour@gmail.com"}
    assert "amira@gmail.com" not in await emails({"has_orders": "no"})
    assert await emails({"incidents": True}) == {"amira@gmail.com"}
    assert await emails({"invited": True}) == {"nour@gmail.com"}
    assert await emails({"signup": "google"}) == {"nour@gmail.com"}
    assert await emails({"q": "98765"}) == {"amira@gmail.com"}
    assert "qa-client-1@example.test" in await emails({"hide_tests": False})
    top = (await call(client, "listAdminUsers", world["admin"], {"sort": "spent"})).json()["users"]
    assert top[0]["email"] == "amira@gmail.com"
