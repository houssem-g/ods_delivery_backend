"""Small pure units: pool budget, rate-limit key, SMTP composition, phones, storage keys, dates."""

import logging
import uuid
from datetime import UTC, datetime

import pytest
from starlette.requests import Request

from app.compat.dates import legacy_datetime, parse_legacy_datetime
from app.config import Settings, settings
from app.db import connect_args, resolve_pool
from app.rate_limit import _rate_key
from app.services import email as email_service
from app.services.phones import InvalidPhone, to_e164
from app.storage import keys
from tests.factories import token_for


def test_pool_is_clamped_to_the_connection_budget():
    cfg = Settings(DB_MAX_CONNECTIONS=25, DB_CONNECTION_RESERVE=5, WEB_CONCURRENCY=2, DEPLOYMENT_REPLICAS=2)
    pool = resolve_pool(cfg)
    assert pool["pool_size"] + pool["max_overflow"] <= (25 - 5) // 4
    starved = resolve_pool(Settings(DB_MAX_CONNECTIONS=4, DB_CONNECTION_RESERVE=2, WEB_CONCURRENCY=4))
    assert starved["pool_size"] == 1 and starved["max_overflow"] == 0
    assert resolve_pool(Settings(DB_NULLPOOL=True))["poolclass"].__name__ == "NullPool"
    assert connect_args(Settings(DB_SSLMODE="require"))["ssl"] is not False


def test_production_refuses_local_secrets():
    with pytest.raises(ValueError):
        Settings(ENVIRONMENT="production")
    ok = Settings(
        ENVIRONMENT="production", JWT_SECRET="x" * 40, COOKIE_SECURE=True, CORS_ORIGINS="https://a,https://b"
    )
    assert ok.CORS_ORIGINS == ["https://a", "https://b"] and not ok.is_local


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {"type": "http", "headers": raw, "client": ("10.0.0.1", 1234), "method": "GET", "path": "/"}
    )


async def test_rate_key_uses_the_user_when_signed_in(factory):
    user = await factory.user()
    assert _rate_key(_request({"Authorization": f"Bearer {token_for(user)}"})) == f"user:{user.id}"
    assert _rate_key(_request({"Authorization": "Bearer broken"})) == "ip:10.0.0.1"
    assert _rate_key(_request({})) == "ip:10.0.0.1"


async def test_smtp_provider_builds_a_multipart_message(monkeypatch):
    sent = {}

    async def fake_send(message, **kwargs):
        sent["message"], sent["kwargs"] = message, kwargs

    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(email_service.aiosmtplib, "send", fake_send)
    await email_service.send_code_email("a@example.test", "reset", "123456", "fr", link_token="tok")
    message = sent["message"]
    assert message["To"] == "a@example.test" and "Réinitialisation" in message["Subject"]
    body = message.get_body(("plain",)).get_content()
    assert "123456" in body and "reset_token=tok" in body
    assert sent["kwargs"]["hostname"] == settings.SMTP_HOST and sent["kwargs"]["port"] == settings.SMTP_PORT


async def test_code_email_failure_is_logged_not_raised(monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(email_service, "send_email", broken)
    with caplog.at_level(logging.ERROR, logger="odsd.email"):
        await email_service.send_code_email("a@example.test", "verify", "123456", "ar")
    assert "verify code e-mail failed" in caplog.text


@pytest.mark.parametrize("raw", ["abc", "+216 12", "12345678901234567890"])
def test_invalid_phones(raw):
    with pytest.raises(InvalidPhone):
        to_e164(raw)


def test_storage_key_rules():
    owner = uuid.uuid4()
    assert keys.build_key("private", "receipt", owner, "jpg").startswith(f"private/receipt/{owner}/")
    assert keys.build_key("public", "shop", owner, "png").startswith("public/shop/")
    assert keys.purpose_of_key("private/courier_id/u/x.jpg") == "courier_id"
    assert keys.purpose_of_key("x") is None
    assert keys.sniff_matches("image/jpeg", b"\xff\xd8\xff\xe0")
    assert keys.sniff_matches("image/gif", b"GIF89a....")
    assert keys.sniff_matches("image/webp", b"RIFF\x00\x00\x00\x00WEBP")
    assert keys.sniff_matches("image/heic", b"\x00\x00\x00\x18ftypheic")
    assert not keys.sniff_matches("text/html", b"<html>")
    assert "application/pdf" in keys.allowed_types("private") and "application/pdf" not in keys.allowed_types(
        "public"
    )


def test_legacy_dates():
    assert legacy_datetime(None) is None
    assert legacy_datetime(datetime(2026, 9, 28, 8, 31, 58, 996000)) == "2026-09-28T08:31:58.996000Z"
    assert parse_legacy_datetime("2026-09-28T08:31:58.996000").tzinfo == UTC
    assert parse_legacy_datetime("2026-09-28T10:31:58+02:00").astimezone(UTC).hour == 8
