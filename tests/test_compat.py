"""Compat framework: filter translator, sort/pagination, read policy, field guards, write dispatch,
and the two worked entities (UserProfile, AppSettings)."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, true

from app.compat.registry import REGISTRY, EntityDef, LegacyField, register
from app.db import SessionLocal
from app.models import NoResponseCase, Order, User, UserAddress
from tests.factories import auth, error_of

ISO_NAIVE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}$")


async def listing(client, user, entity="UserProfile", q=None, **params):
    if q is not None:
        params["q"] = json.dumps(q)
    return await client.get(f"/api/entities/{entity}", params=params, headers=auth(user))


def emails(response) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(doc["user_id"] for doc in response.json())


@pytest.fixture
async def people(factory):
    admin = await factory.user(email="admin@example.test", role="admin")
    ali = await factory.user(email="ali@example.test", language="ar", phone_e164="+21622111222")
    await factory.address(
        ali, address="Rue 1", governorate="Sousse", city="Sousse", location="SRID=4326;POINT(10.6084 35.8256)"
    )
    fatma = await factory.user(email="fatma@example.test", language="fr", is_blacklisted=True)
    nobody = await factory.user(email="noprofile@example.test", profile=False)
    return {"admin": admin, "ali": ali, "fatma": fatma, "nobody": nobody}


# --- filter translator ----------------------------------------------------------------------------


async def test_list_shape_and_dates(client, people):
    response = await listing(client, people["admin"])
    docs = response.json()
    assert emails(response) == ["admin@example.test", "ali@example.test", "fatma@example.test"]
    ali = next(d for d in docs if d["user_id"] == "ali@example.test")
    assert ali["id"] == str(people["ali"].id)
    assert ISO_NAIVE.match(ali["created_date"]) and ISO_NAIVE.match(ali["updated_date"])
    assert ali["created_by"] == "ali@example.test"
    assert ali["default_lat"] == pytest.approx(35.8256) and ali["default_lng"] == pytest.approx(10.6084)
    assert ali["notification_preferences"] == {
        "order_status_changes": True,
        "new_orders": True,
        "incoming_orders": True,
        "chat_messages": True,
        "push_notifications_enabled": True,
    }
    assert ali["is_active"] is True and ali["total_orders"] == 0 and ali["no_response_incidents"] == 0


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ({"language": "fr"}, ["fatma@example.test"]),
        ({"user_id": "ALI@example.test"}, ["ali@example.test"]),  # citext, like e-mail comparisons today
        (
            {"user_id": {"$in": ["ali@example.test", "fatma@example.test"]}},
            ["ali@example.test", "fatma@example.test"],
        ),
        ({"governorate": {"$nin": ["Sousse"]}}, ["admin@example.test", "fatma@example.test"]),
        ({"governorate": {"$nin": ["Sousse", None]}}, []),
        ({"is_blacklisted": {"$ne": True}}, ["admin@example.test", "ali@example.test"]),
        ({"governorate": {"$ne": "Sousse"}}, ["admin@example.test", "fatma@example.test"]),
        ({"governorate": {"$ne": None}}, ["ali@example.test"]),
        ({"governorate": None}, ["admin@example.test", "fatma@example.test"]),
        ({"default_lat": {"$exists": True}}, ["ali@example.test"]),
        ({"default_lat": {"$exists": False}}, ["admin@example.test", "fatma@example.test"]),
        ({"default_lat": {"$gt": 35, "$lt": 36}}, ["ali@example.test"]),
        ({"default_lat": {"$gte": 35.8256, "$lte": 35.8256}}, ["ali@example.test"]),
        ({"default_lat": {"$lt": 0}}, []),
        (
            {"notification_preferences": {"$exists": True}},
            ["admin@example.test", "ali@example.test", "fatma@example.test"],
        ),
        ({"id": "not-a-uuid"}, []),
        ({"id": {"$in": ["not-a-uuid"]}}, []),
        (
            {"referred_by_courier_id": {"$ne": "not-a-uuid"}},
            ["admin@example.test", "ali@example.test", "fatma@example.test"],
        ),
        ({"language": {"$in": ["fr"], "$ne": "ar"}}, ["fatma@example.test"]),
    ],
)
async def test_operators(client, people, q, expected):
    assert emails(await listing(client, people["admin"], q=q)) == expected


async def test_filter_by_id_and_dates(client, people):
    ali = people["ali"]
    assert emails(await listing(client, people["admin"], q={"id": str(ali.id)})) == ["ali@example.test"]
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert emails(await listing(client, people["admin"], q={"created_date": {"$gt": future}})) == []
    past = "2000-01-01T00:00:00.000000"  # naive = UTC, as Base44 writes it
    assert len(emails(await listing(client, people["admin"], q={"created_date": {"$gte": past}}))) == 3


@pytest.mark.parametrize(
    ("q", "message"),
    [
        ({"no_such_field": 1}, "Unknown field"),
        ({"language": {"$regex": "a"}}, "Unsupported operator"),
        ({"language": {}}, "Empty operator"),
        ({"language": ["ar", "fr"]}, "Use $in"),
        ({"language": {"$in": "ar"}}, "expects an array"),
        ({"is_blacklisted": "yes"}, "expected true or false"),
        ({"default_lat": {"$gt": "high"}}, "expected a number"),
        ({"default_lat": {"$gt": None}}, "needs a value"),
        ({"default_lat": {"$exists": "yes"}}, "expects true or false"),
        ({"created_date": {"$gt": "yesterday"}}, "invalid date-time"),
        ({"notification_preferences": {"$ne": {}}}, "only supports $exists"),
    ],
)
async def test_bad_filters_are_400(client, people, q, message):
    response = await listing(client, people["admin"], q=q)
    assert response.status_code == 400 and error_of(response) == "invalid_query"
    assert message in response.json()["message"]


async def test_bad_query_parameters(client, people):
    admin = people["admin"]
    assert (await client.get("/api/entities/UserProfile?q={nope", headers=auth(admin))).status_code == 400
    assert (await client.get("/api/entities/UserProfile?q=[1]", headers=auth(admin))).status_code == 400
    assert (await listing(client, admin, sort="-nope")).status_code == 400
    assert (await listing(client, admin, sort="notification_preferences")).status_code == 400
    assert (await listing(client, admin, limit=-1)).status_code == 400
    assert (await listing(client, admin, skip=-1)).status_code == 400


async def test_sort_limit_skip_and_projection(client, people):
    admin = people["admin"]
    by_email = [d["user_id"] for d in (await listing(client, admin, sort="user_id")).json()]
    assert by_email == ["admin@example.test", "ali@example.test", "fatma@example.test"]
    desc = [d["user_id"] for d in (await listing(client, admin, sort="-user_id")).json()]
    assert desc == list(reversed(by_email))
    page = [d["user_id"] for d in (await listing(client, admin, sort="user_id", limit=1, skip=1)).json()]
    assert page == ["ali@example.test"]
    newest_first = [d["user_id"] for d in (await listing(client, admin, sort="-created_date")).json()]
    assert newest_first[0] == "fatma@example.test"
    projected = (await listing(client, admin, sort="user_id", limit=1, fields="user_id,language")).json()
    assert projected == [{"id": str(admin.id), "user_id": "admin@example.test", "language": "ar"}]


# --- read policy -----------------------------------------------------------------------------------


async def test_read_policy_applies_in_sql(client, people):
    ali, fatma = people["ali"], people["fatma"]
    assert emails(await listing(client, ali)) == ["ali@example.test"]
    assert emails(await listing(client, ali, q={"user_id": fatma.email})) == []
    other = await client.get(f"/api/entities/UserProfile/{fatma.id}", headers=auth(ali))
    assert other.status_code == 404
    own = await client.get(f"/api/entities/UserProfile/{ali.id}", headers=auth(ali))
    assert own.status_code == 200 and own.json()["user_id"] == ali.email
    assert (await client.get("/api/entities/UserProfile")).status_code == 401


async def test_unknown_entity(client, people):
    response = await listing(client, people["admin"], entity="Nope")
    assert response.status_code == 404 and error_of(response) == "unknown_entity"


@pytest.fixture
def guarded_entity():
    """A throwaway entity whose `language` is only visible to its owner."""
    users = User.__table__
    entity = register(
        EntityDef(
            name="GuardedTest",
            source=users,
            id_expr=users.c.id,
            id_type="uuid",
            created_expr=users.c.created_at,
            updated_expr=users.c.updated_at,
            fields={
                "email": LegacyField(users.c.email, "string"),
                "language": LegacyField(users.c.language, "string", read_guard=lambda u: users.c.id == u.id),
            },
            read_policy=lambda _u: true(),
        )
    )
    yield entity
    REGISTRY.pop("GuardedTest")


async def test_field_guard_hides_value_and_filtering(client, people, guarded_entity):
    ali, fatma = people["ali"], people["fatma"]
    docs = {d["email"]: d for d in (await listing(client, ali, entity="GuardedTest")).json()}
    assert docs[ali.email]["language"] == "ar"
    assert docs[fatma.email]["language"] is None
    probe = await listing(client, ali, entity="GuardedTest", q={"language": "fr"})
    assert probe.json() == []  # fatma's hidden value can't be found by filtering either
    for verb, path in (("post", ""), ("patch", f"/{ali.id}"), ("delete", f"/{ali.id}")):
        refused = await getattr(client, verb)(
            f"/api/entities/GuardedTest{path}",
            headers=auth(ali),
            **({"json": {}} if verb != "delete" else {}),
        )
        assert refused.status_code == 403 and error_of(refused) == "permission_denied"
        assert refused.json()["message"].startswith("Permission denied")


# --- UserProfile writes --------------------------------------------------------------------------


async def test_profile_create_is_self_only_and_idempotent(client, factory):
    user = await factory.user(profile=False)
    assert (await client.get(f"/api/entities/UserProfile/{user.id}", headers=auth(user))).status_code == 404
    other = await client.post(
        "/api/entities/UserProfile",
        json={"user_id": "someone@else.test", "role": "customer"},
        headers=auth(user),
    )
    assert other.status_code == 403

    created = await client.post(
        "/api/entities/UserProfile",
        json={
            "user_id": user.email,
            "role": "courier",
            "language": "fr",
            "total_orders": 99,
            "is_active": False,
        },
        headers=auth(user),
    )
    assert created.status_code == 201
    doc = created.json()
    assert doc["id"] == str(user.id) and doc["role"] == "courier" and doc["language"] == "fr"
    assert doc["total_orders"] == 0 and doc["is_active"] is True  # derived / admin-only fields ignored

    again = await client.post(
        "/api/entities/UserProfile", json={"user_id": user.email, "role": "customer"}, headers=auth(user)
    )
    assert again.status_code == 201 and again.json()["id"] == str(user.id)
    assert again.json()["role"] == "customer"


async def test_profile_update_by_owner(client, factory):
    user = await factory.user()
    response = await client.patch(
        f"/api/entities/UserProfile/{user.id}",
        json={
            "id": "ignored",
            "created_date": "ignored",
            "phone": "22 123 456",
            "default_address": "Avenue Habib Bourguiba",
            "country": "tn",
            "governorate": "Sousse",
            "city": "Sahloul",
            "default_lat": 35.83,
            "default_lng": 10.59,
            "whatsapp_opt_in": True,
            "notification_preferences": {"chat_messages": False, "unknown_key": True},
            "is_blacklisted": True,
        },
        headers=auth(user),
    )
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["phone"] == "+21622123456" and doc["country"] == "TN" and doc["city"] == "Sahloul"
    assert doc["default_lat"] == pytest.approx(35.83) and doc["whatsapp_opt_in"] is True
    assert ISO_NAIVE.match(doc["whatsapp_opt_in_at"])
    assert doc["notification_preferences"]["chat_messages"] is False
    assert doc["notification_preferences"]["new_orders"] is True
    assert doc["is_blacklisted"] is False  # admin-only field ignored for the owner

    put = await client.put(
        f"/api/entities/UserProfile/{user.id}",
        json={"whatsapp_opt_in": False, "phone": ""},
        headers=auth(user),
    )
    assert put.status_code == 200
    assert put.json()["whatsapp_opt_in"] is False and put.json()["phone"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"phone": "12"},
        {"role": "admin"},
        {"language": "en"},
        {"country": "Tunisia"},
        {"default_lat": 35.8},
        {"default_lat": 135.0, "default_lng": 10.0},
        {"notification_preferences": {"new_orders": "yes"}},
        {"notification_preferences": "all"},
        {"phone": 22123456.5, "language": 3},
        {"referred_by_courier_id": "not-a-uuid"},
    ],
)
async def test_profile_update_validation(client, factory, body):
    user = await factory.user()
    response = await client.patch(f"/api/entities/UserProfile/{user.id}", json=body, headers=auth(user))
    assert response.status_code == 400 and error_of(response) == "validation_error"


async def test_profile_address_cleared_and_admin_keeps_customer_role(client, factory):
    admin = await factory.user(role="admin")
    await client.patch(
        f"/api/entities/UserProfile/{admin.id}",
        json={"default_lat": 35.0, "default_lng": 10.0, "role": "courier"},
        headers=auth(admin),
    )
    cleared = await client.patch(
        f"/api/entities/UserProfile/{admin.id}",
        json={"default_lat": None, "default_lng": None, "governorate": ""},
        headers=auth(admin),
    )
    doc = cleared.json()
    assert doc["default_lat"] is None and doc["governorate"] is None
    assert doc["role"] == "customer"  # an admin stays admin; his profile reads customer
    async with SessionLocal() as s:
        assert (await s.get(User, admin.id)).role == "admin"


async def test_profile_writes_on_someone_else(client, factory):
    owner, stranger = await factory.user(), await factory.user()
    patch = await client.patch(
        f"/api/entities/UserProfile/{owner.id}", json={"language": "fr"}, headers=auth(stranger)
    )
    assert patch.status_code == 404
    bad_id = await client.patch("/api/entities/UserProfile/xyz", json={}, headers=auth(stranger))
    assert bad_id.status_code == 404
    delete = await client.delete(f"/api/entities/UserProfile/{owner.id}", headers=auth(stranger))
    assert delete.status_code == 403 and error_of(delete) == "permission_denied"


async def test_admin_disables_and_deletes_profiles(client, factory):
    admin, user = await factory.user(role="admin"), await factory.user()
    off = await client.patch(
        f"/api/entities/UserProfile/{user.id}",
        json={"is_active": False, "is_blacklisted": True},
        headers=auth(admin),
    )
    assert (
        off.status_code == 200 and off.json()["is_active"] is False and off.json()["is_blacklisted"] is True
    )
    login = await client.post("/api/auth/login", json={"email": user.email, "password": "Correct-Horse-9"})
    assert error_of(login) == "account_disabled"
    assert (await client.get("/api/auth/me", headers=auth(user))).status_code == 401

    on = await client.patch(
        f"/api/entities/UserProfile/{user.id}", json={"is_active": True}, headers=auth(admin)
    )
    assert on.json()["is_active"] is True
    self_off = await client.patch(
        f"/api/entities/UserProfile/{admin.id}", json={"is_active": False}, headers=auth(admin)
    )
    assert self_off.status_code == 400

    gone = await client.delete(f"/api/entities/UserProfile/{user.id}", headers=auth(admin))
    assert gone.status_code == 200 and gone.json() == {"success": True, "id": str(user.id)}
    assert (await client.get(f"/api/entities/UserProfile/{user.id}", headers=auth(admin))).status_code == 404
    async with SessionLocal() as s:
        assert await s.get(User, user.id) is not None  # the account itself stays


async def test_referral_attribution_is_set_once_and_never_to_oneself(client, factory):
    courier_user = await factory.user()
    courier = await factory.courier(courier_user, referral_code="K7M3Q")
    other_courier = await factory.courier(await factory.user(), referral_code="ZZ999")
    customer = await factory.user(profile=False)

    own = await client.post(
        "/api/entities/UserProfile",
        json={"user_id": courier_user.email, "referred_by_courier_id": str(courier.id)},
        headers=auth(courier_user),
    )
    assert own.json()["referred_by_courier_id"] is None

    first = await client.post(
        "/api/entities/UserProfile",
        json={"user_id": customer.email, "role": "customer", "referred_by_courier_id": str(courier.id)},
        headers=auth(customer),
    )
    doc = first.json()
    assert doc["referred_by_courier_id"] == str(courier.id) and doc["referred_by_code"] == "K7M3Q"
    assert ISO_NAIVE.match(doc["referred_at"])
    later = await client.patch(
        f"/api/entities/UserProfile/{customer.id}",
        json={"referred_by_courier_id": str(other_courier.id), "referred_by_code": "ZZ999"},
        headers=auth(customer),
    )
    assert later.json()["referred_by_courier_id"] == str(courier.id)


async def test_derived_counters_follow_orders_and_the_180_day_incident_rule(client, factory):
    customer = await factory.user()
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        orders = [
            Order(customer_id=customer.id, items_text="bread", contact_name="C", delivery_address="Sousse")
            for _ in range(3)
        ]
        s.add_all(orders)
        await s.flush()

        def case(order: Order, age_days: int, counted: bool) -> NoResponseCase:
            started = now - timedelta(days=age_days)
            return NoResponseCase(
                order_id=order.id,
                status="expired" if counted else "resolved",
                incident_counted=counted,
                started_at=started,
                deadline_at=started,
                final_at=started + timedelta(days=1) if counted else None,
            )

        s.add_all([case(orders[0], 10, True), case(orders[1], 200, True), case(orders[2], 0, False)])
        await s.commit()
    doc = (await client.get(f"/api/entities/UserProfile/{customer.id}", headers=auth(customer))).json()
    assert doc["total_orders"] == 3
    assert doc["no_response_incidents"] == 1  # the 200-day-old incident no longer counts
    assert doc["last_incident_date"].startswith((now - timedelta(days=9)).strftime("%Y-%m-%d"))  # final_at


async def test_default_address_row_is_unique(client, factory):
    user = await factory.user()
    for city in ("A", "B"):
        await client.patch(f"/api/entities/UserProfile/{user.id}", json={"city": city}, headers=auth(user))
    async with SessionLocal() as s:
        rows = (await s.execute(select(UserAddress).where(UserAddress.user_id == user.id))).scalars().all()
    assert [r.city for r in rows] == ["B"]


# --- AppSettings -----------------------------------------------------------------------------------


async def test_app_settings_admin_writes_everyone_reads(client, factory):
    admin, user = await factory.user(role="admin"), await factory.user()
    assert (await listing(client, user, entity="AppSettings", q={"key": "main"})).json() == []
    denied = await client.post(
        "/api/entities/AppSettings", json={"key": "main", "support_phone": "+21622111222"}, headers=auth(user)
    )
    assert denied.status_code == 403 and error_of(denied) == "permission_denied"

    created = await client.post(
        "/api/entities/AppSettings",
        json={
            "key": "main",
            "support_phone": "+21622111222",
            "support_whatsapp": "",
            "updated_by": "x@evil.test",
        },
        headers=auth(admin),
    )
    assert created.status_code == 201
    doc = created.json()
    assert doc["id"] == "main" and doc["updated_by"] == admin.email and doc["support_phone"] == "+21622111222"

    rows = (
        await listing(client, user, entity="AppSettings", q={"key": "main"}, sort="-updated_date", limit=1)
    ).json()
    assert rows[0]["support_phone"] == "+21622111222"

    updated = await client.patch(
        "/api/entities/AppSettings/main", json={"support_whatsapp": "+21698765432"}, headers=auth(admin)
    )
    assert updated.json()["support_whatsapp"] == "+21698765432"
    assert updated.json()["support_phone"] == "+21622111222"
    assert (
        await client.patch("/api/entities/AppSettings/main", json={}, headers=auth(user))
    ).status_code == 403
    assert (
        await client.patch("/api/entities/AppSettings/other", json={}, headers=auth(admin))
    ).status_code == 404
    too_long = await client.patch(
        "/api/entities/AppSettings/main", json={"support_phone": "9" * 40}, headers=auth(admin)
    )
    assert too_long.status_code == 400
    no_key = await client.post("/api/entities/AppSettings", json={"support_phone": "1"}, headers=auth(admin))
    assert no_key.status_code == 400

    assert (await client.delete("/api/entities/AppSettings/main", headers=auth(user))).status_code == 403
    assert (await client.delete("/api/entities/AppSettings/main", headers=auth(admin))).status_code == 200
    assert (await client.delete("/api/entities/AppSettings/main", headers=auth(admin))).status_code == 404


async def test_non_object_body_is_ignored_like_base44(client, factory):
    admin = await factory.user(role="admin")
    response = await client.post("/api/entities/AppSettings", json=["key"], headers=auth(admin))
    assert response.status_code == 400  # no key


def test_money_is_serialized_as_numbers():
    from app.compat.values import to_json

    assert to_json(Decimal("12.500"), "number") == 12.5
    assert to_json(Decimal("3"), "integer") == 3
