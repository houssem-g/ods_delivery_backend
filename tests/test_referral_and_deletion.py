"""resolveReferralCode, getCourierReferralStats and deleteMyAccount (anonymization)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AuditLog,
    Courier,
    DeviceToken,
    EmailCode,
    File,
    HotDeal,
    Message,
    Notification,
    Order,
    OrderOffer,
    OrderRating,
    OrderStop,
    OrderTracking,
    RefreshToken,
    ShopReview,
    User,
    UserAddress,
)
from app.services import account_deletion, referral
from tests.catalog_data import wkt
from tests.factories import auth, error_of

NOW = datetime.now(UTC)


async def add(*rows):
    async with SessionLocal() as s:
        s.add_all(rows)
        await s.commit()
        for row in rows:
            await s.refresh(row)
    return rows[0] if len(rows) == 1 else rows


def order(customer: User, courier: Courier | None = None, status: str = "delivered", **fields) -> Order:
    extra = {"delivered_at": NOW} if status == "delivered" else {}
    if status == "cancelled":
        extra = {"cancelled_at": NOW, "cancelled_by": "customer"}
    return Order(
        customer_id=customer.id, courier_id=courier.id if courier else None, status=status, items_text="Pain",
        contact_name="Sami", contact_phone_e164="+21622000111", delivery_address="12 rue X",
        delivery_details="2e étage", delivery_location=wkt(36.8, 10.18), notes="sonnez", **extra, **fields,
    )  # fmt: skip


# --- resolveReferralCode ----------------------------------------------------------------------


def test_code_helpers():
    assert referral.normalize_code(" k7m3q ") == "K7M3Q"
    for bad in ("K7M3", "K0M3Q", "KIM3Q", "ABCDEFGHJ", None, 12345):
        assert referral.normalize_code(bad) is None
    assert referral.first_name("  Sami  Ben Ali ") == "Sami" and referral.first_name(None) == ""
    code = referral.random_code(6)
    assert len(code) == 6 and referral.CODE_RE.match(code)


async def test_resolve_referral_code(client, factory):
    owner = await factory.user()
    courier = await factory.courier(owner, display_name="Sami Ben Ali", referral_code="K7M3Q", vehicle="car")
    url = "/api/functions/resolveReferralCode"

    anonymous = await client.post(url, json={"code": "k7m3q"})
    assert anonymous.status_code == 200
    assert anonymous.json() == {
        "valid": True, "code": "K7M3Q", "courier_id": str(courier.id), "first_name": "Sami", "rating": None,
        "total_deliveries": 0, "vehicle_type": "car",
    }  # fmt: skip
    assert (await client.post(url, json={"code": "nope!"})).json() == {
        "valid": False,
        "reason": "invalid_code",
    }
    assert (await client.post(url, json={})).json() == {"valid": False, "reason": "invalid_code"}
    assert (await client.post(url, json={"code": "ZZZZZ"})).json() == {
        "valid": False,
        "reason": "unknown_code",
    }
    own = await client.post(url, json={"code": "K7M3Q"}, headers=auth(owner))
    assert own.json() == {"valid": False, "reason": "self", "is_self": True}

    customer = await factory.user()
    delivered = await add(order(customer, courier))
    await add(OrderRating(order_id=delivered.id, courier_id=courier.id, rater_id=customer.id, rating=4))
    rated = (await client.post(url, json={"code": "K7M3Q"}, headers=auth(customer))).json()
    assert rated["rating"] == 4.0 and rated["total_deliveries"] == 1

    async with SessionLocal() as s:
        row = await s.get(Courier, courier.id)
        row.verification = "rejected"
        await s.commit()
    assert (await client.post(url, json={"code": "K7M3Q"})).json()["reason"] == "unknown_code"


# --- getCourierReferralStats --------------------------------------------------------------------


async def test_courier_referral_stats(client, factory):
    url = "/api/functions/getCourierReferralStats"
    plain = await factory.user()
    assert (await client.post(url)).status_code == 401
    missing = await client.post(url, headers=auth(plain))
    assert missing.status_code == 404 and error_of(missing) == "no_courier_profile"

    owner = await factory.user()
    courier = await factory.courier(owner, verification="verified")
    other_owner = await factory.user()
    other = await factory.courier(other_owner, phone_e164="+21622999888", referral_code="QWERT")

    first = (await client.post(url, headers=auth(owner))).json()
    code = first["code"]
    assert referral.CODE_RE.match(code) and len(code) == 5
    assert first == {
        "code": code, "verified": True, "referred_count": 0, "ordered_count": 0, "delivered_orders": 0,
        "delivered_by_me": 0,
    }  # fmt: skip
    assert (await client.post(url, headers=auth(owner))).json()["code"] == code  # stable

    async def referred(created=NOW, **extra):
        user = await factory.user(referred_by_courier_id=courier.id, referred_at=NOW, **extra)
        async with SessionLocal() as s:
            row = await s.get(User, user.id)
            row.profile_created_at = created
            await s.commit()
        return user

    signed_up = await referred()
    also = await referred(created=NOW - timedelta(hours=20))
    await referred(created=NOW - timedelta(days=3))  # attribution added later: not counted
    await referred(deleted_at=NOW)  # deleted account
    await referred(created=None)  # no customer profile
    await factory.user(referred_by_courier_id=courier.id, referred_at=None)
    async with SessionLocal() as s:  # his own account attributed to himself
        me = await s.get(User, owner.id)
        me.referred_by_courier_id, me.referred_at, me.profile_created_at = courier.id, NOW, NOW
        await s.commit()
    await add(
        order(signed_up, courier, preferred_courier_id=courier.id),
        order(signed_up, other, preferred_courier_id=courier.id),
        order(also, None, status="cancelled", preferred_courier_id=courier.id),
        order(also, courier),  # not through his link (no preferred courier)
    )
    stats = (await client.post(url, headers=auth(owner))).json()
    assert stats["referred_count"] == 2 and stats["ordered_count"] == 1
    assert stats["delivered_orders"] == 2 and stats["delivered_by_me"] == 1


async def test_referral_code_normalized_and_regenerated(client, factory, monkeypatch):
    owner = await factory.user()
    await factory.courier(owner, referral_code="abcde")
    assert (await client.post("/api/functions/getCourierReferralStats", headers=auth(owner))).json()[
        "code"
    ] == "ABCDE"

    owner2 = await factory.user()
    await factory.courier(owner2, phone_e164="+21622999888", referral_code="bad code!")
    codes = iter(["ABCDE", "ABCDE", "ABCDE", "ABCDE", "ABCDE", "HJKMNP"])
    monkeypatch.setattr(referral, "random_code", lambda length: next(codes))
    assert (await client.post("/api/functions/getCourierReferralStats", headers=auth(owner2))).json()[
        "code"
    ] == ("HJKMNP")

    owner3 = await factory.user()
    await factory.courier(owner3, phone_e164="+21622999777")
    monkeypatch.setattr(referral, "random_code", lambda length: "ABCDE")
    stuck = await client.post("/api/functions/getCourierReferralStats", headers=auth(owner3))
    assert stuck.status_code == 500 and error_of(stuck) == "referral_code_unavailable"


# --- deleteMyAccount ---------------------------------------------------------------------------

URL = "/api/functions/deleteMyAccount"


@pytest.fixture
def deleted_objects(monkeypatch):
    keys: list[str] = []

    async def fake_delete(key: str) -> None:
        if key.endswith("fail.jpg"):
            raise RuntimeError("bucket down")
        keys.append(key)

    monkeypatch.setattr(account_deletion.s3, "delete_object", fake_delete)
    return keys


@pytest.mark.parametrize("status", ["pending", "on_the_way", "client_no_response"])
async def test_refused_while_an_order_runs(client, factory, status):
    customer = await factory.user()
    courier_user = await factory.user()
    courier = await factory.courier(courier_user)
    running = await add(order(customer, None if status == "pending" else courier, status=status))
    for who in (customer, courier_user) if status != "pending" else (customer,):
        r = await client.post(URL, headers=auth(who))
        assert r.status_code == 409 and error_of(r) == "order_in_progress"
        assert r.json()["orders"] == [str(running.id)]
    async with SessionLocal() as s:
        assert (await s.get(User, customer.id)).deleted_at is None


async def test_customer_deletion_anonymizes(client, factory, deleted_objects):
    customer = await factory.user(full_name="Sami Customer", phone_e164="+21622000111", google_sub="g-1")
    await factory.address(customer, address="12 rue X", location=wkt(36.8, 10.18))
    courier_user = await factory.user()
    courier = await factory.courier(courier_user)
    done = await add(order(customer, courier))
    await add(
        Message(order_id=done.id, sender_id=customer.id, recipient_id=courier_user.id, sender_role="customer",
                body="mon code 1234"),
        Message(order_id=done.id, sender_id=courier_user.id, recipient_id=customer.id, sender_role="courier",
                body="ok"),
        Notification(user_id=customer.id, type="delivered", title_ar="t", title_fr="t"),
        DeviceToken(user_id=customer.id, token="tok-1", platform="android"),
        EmailCode(user_id=customer.id, purpose="reset", code_hash="x", expires_at=NOW + timedelta(minutes=5)),
        RefreshToken(user_id=customer.id, token_hash="h1", family=customer.id,
                     expires_at=NOW + timedelta(days=1)),
        ShopReview(user_id=customer.id, target_key="k", rating=5),
        File(key="public/review/x.jpg", owner_id=customer.id, visibility="public", content_type="image/jpeg",
             size_bytes=1),
    )  # fmt: skip
    old_email = customer.email

    r = await client.post(URL, headers=auth(customer))
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["login_deleted"] is True
    assert body["deleted"] | {} == {
        "offers": 0, "device_tokens": 1, "notifications": 1, "customer_orders": 0, "messages_redacted": 1,
        "courier_orders_anonymised": 0, "courier_profiles": 0, "user_profiles": 1,
        "customer_orders_anonymised": 1, "private_files": 0,
    }  # fmt: skip
    assert deleted_objects == []

    async with SessionLocal() as s:
        user = await s.get(User, customer.id)
        assert user.email == account_deletion.deleted_email(customer.id) and user.email != old_email
        assert (
            user.full_name == "Utilisateur supprimé" and user.phone_e164 is None and user.google_sub is None
        )
        assert (
            user.password_hash is None
            and user.deleted_at
            and user.disabled_at
            and user.profile_created_at is None
        )
        kept = await s.get(Order, done.id)
        assert kept.contact_name == "Client supprimé" and kept.contact_phone_e164 is None
        assert kept.delivery_address == "[adresse supprimée]" and kept.delivery_location is None
        assert kept.delivery_details is None and kept.notes is None and kept.items_text == "Pain"
        bodies = sorted(m.body for m in (await s.execute(select(Message))).scalars())
        assert bodies == ["[message supprimé]", "ok"]
        assert (await s.execute(select(Notification))).first() is None
        token = (await s.execute(select(DeviceToken))).scalar_one()
        assert token.is_active is False and token.last_error == "account_deleted"
        assert (await s.execute(select(UserAddress))).first() is None
        assert (await s.execute(select(EmailCode))).first() is None
        assert (await s.execute(select(RefreshToken))).scalar_one().revoked_at is not None
        assert (await s.execute(select(ShopReview))).scalar_one().user_id == customer.id  # stays, anonymous
        audit = (await s.execute(select(AuditLog))).scalar_one()
        assert audit.action == "account_deleted" and audit.after["customer_orders_anonymised"] == 1

    # the session is over and the address can sign up again
    assert (await client.post(URL, headers=auth(customer))).status_code == 401
    assert (await client.get("/api/auth/me", headers=auth(customer))).status_code == 401


async def test_courier_deletion_keeps_orders_and_erases_documents(client, factory, deleted_objects):
    courier_user = await factory.user(role="courier", profile=False)
    courier = await factory.courier(
        courier_user, display_name="Sami Livreur", referral_code="K7M3Q", is_online=True,
        last_location=wkt(36.8, 10.18), id_document_key="private/courier_id/x/id.jpg",
    )  # fmt: skip
    customer = await factory.user()
    delivered = await add(order(customer, courier))
    open_order = await add(order(customer, None, status="offers_received"))
    await add(
        OrderOffer(order_id=open_order.id, courier_id=courier.id, proposed_fee=Decimal(5), status="pending"),
        OrderOffer(order_id=delivered.id, courier_id=courier.id, proposed_fee=Decimal(5), status="accepted"),
        OrderTracking(order_id=delivered.id, courier_id=courier.id, location=wkt(36.8, 10.18),
                      recorded_at=NOW),
        OrderStop(order_id=delivered.id, seq=0, name="Shop", receipt_key="private/receipt/x/r.jpg"),
        File(key="private/receipt/x/r.jpg", owner_id=courier_user.id, visibility="private",
             content_type="image/jpeg", size_bytes=1),
        File(key="private/courier_id/x/back.jpg", owner_id=courier_user.id, visibility="private",
             content_type="image/jpeg", size_bytes=1),
        File(key="private/courier_id/x/fail.jpg", owner_id=courier_user.id, visibility="private",
             content_type="image/jpeg", size_bytes=1),
        HotDeal(original_order_id=delivered.id, courier_id=courier.id, items_text="Pain",
                purchase_amount=Decimal(10), discount_percentage=Decimal(20), price=Decimal(8),
                expires_at=NOW + timedelta(hours=2)),
    )  # fmt: skip

    r = await client.post(URL, headers=auth(courier_user))
    assert r.status_code == 200, r.text
    deleted = r.json()["deleted"]
    assert deleted["offers"] == 1 and deleted["courier_profiles"] == 1 and deleted["user_profiles"] == 0
    assert deleted["courier_orders_anonymised"] == 1 and deleted["customer_orders_anonymised"] == 0
    assert deleted["private_files"] == 2  # 3 keys, one delete failed (best effort)
    assert sorted(deleted_objects) == ["private/courier_id/x/back.jpg", "private/courier_id/x/id.jpg"]

    async with SessionLocal() as s:
        row = await s.get(Courier, courier.id)
        assert row.display_name == "Livreur supprimé" and row.phone_e164 is None
        assert row.id_document_number == "" and row.id_document_key is None and row.referral_code is None
        assert row.is_online is False and row.last_location is None and row.verification == "rejected"
        offers = (await s.execute(select(OrderOffer))).scalars().all()
        assert [o.status for o in offers] == ["accepted"]
        assert (await s.execute(select(OrderTracking))).first() is None
        assert (await s.execute(select(HotDeal))).scalar_one().status == "expired"
        assert (await s.get(Order, delivered.id)).courier_id == courier.id
        # its last pending offer is gone: the open order is waiting again (offers.demote_if_no_pending_offer)
        assert (await s.get(Order, open_order.id)).status == "pending"
        files = sorted(f.key for f in (await s.execute(select(File))).scalars())
        assert files == ["private/receipt/x/r.jpg"]  # the receipt stays with the order


async def test_deleted_account_cannot_delete_again(client, factory, deleted_objects):
    user = await factory.user()
    from app.security.deps import CurrentUser

    async with SessionLocal() as s:
        await account_deletion.delete_account(s, CurrentUser(user.id, user.email, user.role, user.full_name))
        await s.commit()
    async with SessionLocal() as s:
        with pytest.raises(account_deletion.ApiError) as exc:
            await account_deletion.delete_account(s, CurrentUser(user.id, "x", "customer", ""))
    assert exc.value.status == 404
