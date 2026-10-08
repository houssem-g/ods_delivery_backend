"""« Se connecter avec l'empreinte / le visage »: device keys enrolled after a normal sign-in."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import DeviceKey, User
from app.services.device_keys import MAX_KEYS
from tests.factories import PASSWORD, auth, error_of

NEW_PASSWORD = "An0ther-Strong-Pass!"


async def enroll(client, user, label="Pixel 8"):
    res = await client.post("/api/auth/device/enroll", json={"label": label}, headers=auth(user))
    assert res.status_code == 201
    return res.json()


async def test_enroll_then_sign_in_with_the_phone(client, factory):
    user = await factory.user()
    key = await enroll(client, user)
    assert key["device_id"] and len(key["secret"]) >= 40

    res = await client.post("/api/auth/device/login", json=key)
    assert res.status_code == 200
    body = res.json()
    assert body["access_token"] and body["user"]["email"] == user.email
    assert client.cookies[settings.REFRESH_COOKIE_NAME]  # a normal session: refresh works
    assert (await client.post("/api/auth/refresh")).status_code == 200

    async with SessionLocal() as s:
        row = await s.get(DeviceKey, uuid.UUID(key["device_id"]))
        assert row.secret_hash != key["secret"] and row.last_used_at is not None and row.label == "Pixel 8"


async def test_enroll_needs_a_signed_in_user(client):
    assert (await client.post("/api/auth/device/enroll", json={})).status_code == 401


async def test_wrong_secret_revokes_the_key(client, factory):
    user = await factory.user()
    key = await enroll(client, user)
    bad = await client.post("/api/auth/device/login", json={"device_id": key["device_id"], "secret": "guess"})
    assert bad.status_code == 401 and error_of(bad) == "device_key_invalid"
    # even the right secret is refused afterwards: the phone falls back to the password
    again = await client.post("/api/auth/device/login", json=key)
    assert again.status_code == 401


async def test_unknown_or_malformed_key(client):
    for body in (
        {"device_id": "not-a-uuid", "secret": "x"},
        {"device_id": "00000000-0000-0000-0000-000000000000", "secret": "x"},
    ):
        res = await client.post("/api/auth/device/login", json=body)
        assert res.status_code == 401 and error_of(res) == "device_key_invalid"


async def test_new_password_and_disabled_account_end_the_phone_sign_in(client, factory):
    user = await factory.user()
    key = await enroll(client, user)

    changed = await client.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=auth(user),
    )
    assert changed.status_code == 200
    assert (await client.post("/api/auth/device/login", json=key)).status_code == 401

    other = await factory.user()
    key2 = await enroll(client, other)
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == other.id).values(disabled_at=datetime.now(UTC)))
        await s.commit()
    assert (await client.post("/api/auth/device/login", json=key2)).status_code == 401


async def test_logout_keeps_the_phone_sign_in(client, factory):
    user = await factory.user()
    key = await enroll(client, user)
    await client.post("/api/auth/device/login", json=key)
    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.post("/api/auth/device/login", json=key)).status_code == 200


async def test_user_turns_it_off_only_for_his_own_keys(client, factory):
    user, stranger = await factory.user(), await factory.user()
    key = await enroll(client, user)
    nope = await client.post(
        "/api/auth/device/revoke", json={"device_id": key["device_id"]}, headers=auth(stranger)
    )
    assert nope.status_code == 200 and nope.json()["revoked"] is False
    assert (await client.post("/api/auth/device/login", json=key)).status_code == 200
    off = await client.post(
        "/api/auth/device/revoke", json={"device_id": key["device_id"]}, headers=auth(user)
    )
    assert off.json()["revoked"] is True
    assert (await client.post("/api/auth/device/login", json=key)).status_code == 401


async def test_at_most_max_keys_alive(client, factory):
    user = await factory.user()
    keys = [await enroll(client, user, f"phone {i}") for i in range(MAX_KEYS + 2)]
    async with SessionLocal() as s:
        alive = await s.scalar(
            select(func.count())
            .select_from(DeviceKey)
            .where(DeviceKey.user_id == user.id, DeviceKey.revoked_at.is_(None))
        )
    assert alive == MAX_KEYS
    assert (await client.post("/api/auth/device/login", json=keys[0])).status_code == 401
    assert (await client.post("/api/auth/device/login", json=keys[-1])).status_code == 200
