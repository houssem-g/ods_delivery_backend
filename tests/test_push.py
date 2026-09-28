import asyncio
import time
from types import SimpleNamespace

import pytest
from firebase_admin import exceptions as fb_exceptions
from firebase_admin import messaging
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import DeviceToken, PushDelivery
from app.services import push
from app.services.push import FcmProvider, LogProvider, PushMessage, push_link, recipient_role, send_to_user

MESSAGE = PushMessage(
    type="new_offer", title_ar="عرض", title_fr="Offre", body_ar="جديد", body_fr="Nouvelle offre",
    order_id="o-1", metadata={"offer_id": "x"},
)  # fmt: skip


@pytest.mark.parametrize(
    ("type_", "metadata", "expected"),
    [
        ("new_order", {}, "courier"),
        ("new_offer", {}, "customer"),
        ("new_message", {"sender_role": "courier"}, "customer"),
        ("new_message", {"sender_role": "customer"}, "courier"),
        ("new_message", {}, None),
        ("order_accepted", {"status": "accepted"}, "customer"),
        ("order_accepted", {"offer_id": "x"}, "courier"),
        ("order_accepted", {}, None),
        ("order_cancelled", {"ctx": 1}, "customer"),
        ("order_cancelled", {}, "courier"),
        ("delivered", {"recipient_role": "courier"}, "courier"),
        ("something_else", {}, None),
    ],
)
def test_recipient_role(type_, metadata, expected):
    assert recipient_role(type_, metadata) == expected


def test_push_links():
    assert push_link("new_order", "o1", {}) == "/CourierOrderDetail?id=o1&as=courier"
    assert push_link("customer_responded", "o1", {}) == "/CourierOrderActive?id=o1&as=courier"
    assert push_link("new_order", None, {}) == "/CourierHome?as=courier"
    assert push_link("new_offer", "o1", {}) == "/OrderOffers?id=o1&as=customer"
    assert push_link("delivered", "o1", {}) == "/OrderTracking?id=o1&as=customer"
    assert push_link("delivered", None, {}) == "/?as=customer"
    assert push_link("mystery", None, {}) == "/"


def test_multicast_payload_matches_the_deno_function():
    msg = PushMessage(**{**MESSAGE.__dict__, "type": "new_order"})
    built = push.build_multicast(["t1"], "fr", msg)
    assert built.notification.title == "Offre" and built.data["click_action"].startswith(
        "/CourierOrderDetail"
    )
    assert built.android.notification.channel_id == "new_orders" and built.android.collapse_key == "order_o-1"
    assert built.webpush.notification.require_interaction is True
    assert built.data["metadata_json"] == '{"offer_id": "x"}'
    assert push.build_multicast(["t1"], "ar", MESSAGE).notification.title == "عرض"


async def _tokens(user, *specs):
    async with SessionLocal() as s:
        rows = [DeviceToken(user_id=user.id, platform="android", **spec) for spec in specs]
        s.add_all(rows)
        await s.commit()
        return [r.id for r in rows]


async def test_log_provider_records_deliveries(factory):
    user = await factory.user()
    await _tokens(user, {"token": "a", "failure_count": 2, "last_error": "old"}, {"token": "b"},
                  {"token": "dead", "is_active": False})  # fmt: skip
    async with SessionLocal() as s:
        result = await send_to_user(s, user.id, MESSAGE, LogProvider())
        await s.commit()
        assert result == {"provider": "log", "attempted": 2, "delivered": 2}
        rows = (await s.execute(select(PushDelivery))).scalars().all()
        token_a = (await s.execute(select(DeviceToken).where(DeviceToken.token == "a"))).scalar_one()
    assert {r.provider for r in rows} == {"log"} and len(rows) == 2
    assert rows[0].payload["link"] == "/OrderOffers?id=o-1&as=customer"
    assert token_a.failure_count == 0 and token_a.last_error is None


async def test_no_devices(factory):
    user = await factory.user()
    async with SessionLocal() as s:
        assert (await send_to_user(s, user.id, MESSAGE, LogProvider()))["attempted"] == 0


async def test_fcm_provider_upkeeps_tokens(factory, monkeypatch):
    user = await factory.user()
    await _tokens(
        user,
        {"token": "ok", "locale": "fr"},
        {"token": "unregistered"},
        {"token": "invalid"},
        {"token": "flaky", "failure_count": 4},
        {"token": "flaky-once"},
    )
    calls = []

    def fake_send(multicast):
        calls.append(multicast)
        outcomes = {
            "ok": SimpleNamespace(success=True, exception=None),
            "unregistered": SimpleNamespace(success=False, exception=messaging.UnregisteredError("gone")),
            "invalid": SimpleNamespace(success=False, exception=fb_exceptions.InvalidArgumentError("bad")),
            "flaky": SimpleNamespace(success=False, exception=fb_exceptions.UnavailableError("later")),
            "flaky-once": SimpleNamespace(success=False, exception=fb_exceptions.UnavailableError("later")),
        }
        return SimpleNamespace(responses=[outcomes[t] for t in multicast.tokens])

    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_send)
    async with SessionLocal() as s:
        result = await send_to_user(s, user.id, MESSAGE, FcmProvider())
        await s.commit()
        tokens = {t.token: t for t in (await s.execute(select(DeviceToken))).scalars()}
        statuses = sorted(r.status for r in (await s.execute(select(PushDelivery))).scalars())
    assert result == {"provider": "fcm", "attempted": 5, "delivered": 1}
    assert sorted(len(c.tokens) for c in calls) == [1, 4]  # one multicast per locale
    assert tokens["unregistered"].is_active is False and tokens["invalid"].is_active is False
    assert tokens["flaky"].is_active is False and tokens["flaky"].failure_count == 5
    assert tokens["flaky-once"].is_active is True and tokens["flaky-once"].failure_count == 1
    assert statuses == ["failed", "failed", "invalid_token", "invalid_token", "sent"]


async def test_fcm_timeout_marks_the_batch_failed(factory, monkeypatch):
    user = await factory.user()
    await _tokens(user, {"token": "slow"})
    monkeypatch.setattr(settings, "PUSH_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(messaging, "send_each_for_multicast", lambda m: time.sleep(0.5))
    async with SessionLocal() as s:
        result = await send_to_user(s, user.id, MESSAGE, FcmProvider())
        delivery = (await s.execute(select(PushDelivery))).scalar_one()
    assert result["delivered"] == 0 and delivery.error == "timeout"
    await asyncio.sleep(0.5)  # let the worker thread finish


def test_provider_choice_and_firebase_init(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PUSH_PROVIDER", "log")
    assert isinstance(push.get_provider(), LogProvider)
    monkeypatch.setattr(settings, "PUSH_PROVIDER", "fcm")
    assert isinstance(push.get_provider(), FcmProvider)
    monkeypatch.setattr(settings, "PUSH_PROVIDER", "auto")
    assert isinstance(push.get_provider(), LogProvider)  # no Firebase app in tests

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_PATH", None)
    assert push.init_firebase() is False
    broken = tmp_path / "creds.json"
    broken.write_text("{}")
    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_PATH", str(broken))
    assert push.init_firebase() is False
