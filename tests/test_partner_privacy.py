"""The partner webhook secret encrypted at rest (Fernet, PARTNER_SECRET_KEY): stored as
`fernet:<token>`, decrypted to sign (signature unchanged for the partner), never used in clear."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from app.config import settings
from app.db import SessionLocal
from app.models import Partner, PartnerWebhookDelivery
from app.security import secret_box
from tests.factories import auth
from tests.order_helpers import OrderWorld, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, WebhookServer, make_partner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
def server():
    return WebhookServer().install()


@pytest.fixture
async def pw(client, world):
    return await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)


async def fn(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def stored_secret(partner_id) -> str:
    return (await reload(Partner, partner_id)).webhook_secret


def test_secret_box_round_trip_prefix_and_key_rotation():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    stored = secret_box.encrypt("s3cret", old)
    assert stored.startswith("fernet:") and "s3cret" not in stored
    assert secret_box.decrypt(stored, old) == "s3cret"
    assert secret_box.decrypt(stored, f"{new},{old}") == "s3cret"  # rotation: new encrypts, old still reads
    assert secret_box.decrypt(secret_box.encrypt("x", f"{new},{old}"), new) == "x"
    with pytest.raises(secret_box.SecretKeyError, match="changed"):
        secret_box.decrypt(stored, new)
    with pytest.raises(secret_box.SecretKeyError, match="not encrypted"):  # never clear text as a fallback
        secret_box.decrypt("s3cret", old)
    with pytest.raises(secret_box.SecretKeyError, match="not configured"):
        secret_box.encrypt("s3cret", "")
    assert secret_box.encrypt(None) is None and secret_box.decrypt(None) is None


async def test_webhook_secret_is_encrypted_at_rest_and_the_signature_does_not_change(
    client, world, pw, server
):
    stored = await stored_secret(pw.partner.id)
    assert stored.startswith("fernet:") and pw.webhook_secret not in stored
    assert secret_box.decrypt(stored) == pw.webhook_secret
    r = await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)
    assert r.json()["delivered"] is True
    assert server.verify(server.calls[-1], pw.webhook_secret)  # the partner checks with the clear secret

    rotated = (
        await fn(client, world.admin, "rotatePartnerWebhookSecret", {"partner_id": str(pw.partner.id)})
    ).json()["webhook_secret"]
    stored = await stored_secret(pw.partner.id)
    assert stored.startswith("fernet:") and rotated not in stored and secret_box.decrypt(stored) == rotated
    await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)
    assert server.verify(server.calls[-1], rotated) and not server.verify(server.calls[-1], pw.webhook_secret)


async def test_a_clear_or_unreadable_stored_secret_is_never_used_to_sign(client, world, pw, server):
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE partners SET webhook_secret = :v WHERE id = :id"),
            {"v": pw.webhook_secret, "id": pw.partner.id},
        )
        await s.commit()
    r = (await client.post(f"{PARTNER_API}/webhook-test", headers=pw.headers)).json()
    assert r["delivered"] is False and r["error"] == "secret_unreadable" and server.calls == []
    [row] = await rows(select(PartnerWebhookDelivery))
    assert row.status == "pending"  # retried once the key / secret is fixed


def _migration():
    return importlib.import_module(
        "app.migrations.versions.partner_20261016_ab1cb4ca5ec5_webhook_secret_encrypted"
    )


def test_migration_refuses_to_run_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "PARTNER_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="PARTNER_SECRET_KEY is not set"):
        _migration()._box()
    monkeypatch.setattr(settings, "PARTNER_SECRET_KEY", "nope")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        _migration()._box()
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "PARTNER_SECRET_KEY", key)
    token = _migration()._box().encrypt(b"abc").decode()
    assert secret_box.decrypt(_migration().PREFIX + token, key) == "abc"  # what the app reads back


def test_alembic_outside_local_stops_before_the_database_without_the_key():
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "JWT_SECRET": "x" * 40,
        "COOKIE_SECURE": "true",
        "PARTNER_PHONE_HASH_SECRET": "y" * 40,
        "PARTNER_SECRET_KEY": "",
        "DATABASE_URL": "postgresql+asyncpg://nobody:nothing@127.0.0.1:9/none",
    }
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0 and "PARTNER_SECRET_KEY must be set" in done.stderr
