"""sendNotificationIfEnabled, notify() (preferences, WhatsApp fallback), the Notification,
DeviceToken and MessageLog entities, registerDeviceToken / unregisterDeviceToken."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import DeviceToken, Notification, OutboundMessage, PushDelivery, User
from app.security.deps import to_current_user
from app.services import device_tokens, notifications
from app.services.notifications import canonical_type, notify_detailed, push_skip_reason
from tests.factories import auth
from tests.messaging_factories import make_device, make_offer, make_order, ok_wa


async def send(client, caller, **payload):
    payload.setdefault("title_fr", "Titre")
    payload.setdefault("title_ar", "عنوان")
    return await client.post("/api/functions/sendNotificationIfEnabled", json=payload, headers=auth(caller))


async def rows(model, *where):
    async with SessionLocal() as s:
        return list((await s.execute(select(model).where(*where))).scalars())


# ─────────────────────────── sendNotificationIfEnabled ───────────────────────────


async def test_input_validation(client, parties):
    me = parties.customer
    cases = [
        ({"type": "new_message"}, 400, "Missing required fields"),
        ({"userId": me.email}, 400, "Missing required fields"),
        ({"userId": me.email, "type": "account_verified"}, 400, "invalid_type"),
        ({"userId": "not an email!", "type": "new_message"}, 400, "invalid_user_id"),
        ({"userId": str(uuid.uuid4()), "type": "new_message"}, 400, "invalid_user_id"),
        ({"userId": me.email, "type": "new_message", "order_id": 12}, 400, "invalid_order_id"),
        (
            {"userId": me.email, "type": "new_message", "metadata": {"x": "y" * 2100}},
            400,
            "metadata_too_large",
        ),
    ]
    for payload, status, error in cases:
        res = await send(client, me, **payload)
        assert (res.status_code, res.json()["error"]) == (status, error), payload
    anonymous = await client.post("/api/functions/sendNotificationIfEnabled", json={})
    assert anonymous.status_code == 401


async def test_self_and_admin(client, parties):
    mine = await send(
        client,
        parties.customer,
        userId="CLIENT@example.test",
        type="message",
        body_fr="b" * 1200,
        metadata=["not", "an", "object"],
    )
    assert mine.status_code == 200 and mine.json()["success"] is True
    [row] = await rows(Notification, Notification.user_id == parties.customer.id)
    assert str(row.id) == mine.json()["notification_id"]
    assert row.type == "new_message" and len(row.body_fr) == 1000 and row.data == {}
    admin = await send(client, parties.admin, userId=parties.stranger.email, type="eta_update")
    assert admin.status_code == 200
    unknown = await send(client, parties.admin, userId="ghost@example.test", type="eta_update")
    assert (unknown.status_code, unknown.json()) == (400, {"error": "invalid_user_id"})
    stranger = await send(client, parties.stranger, userId=parties.customer.email, type="new_message")
    assert (stranger.status_code, stranger.json()) == (403, {"error": "Forbidden"})


async def test_parties_notify_the_other_party_with_allowed_types(client, parties):
    order = await make_order(parties.customer, parties.courier)
    oid = str(order.id)
    customer, courier = parties.customer, parties.courier_user
    allowed = [
        (customer, courier.email, "order_accepted"),
        (customer, courier.email, "order_cancelled"),
        (customer, courier.email, "message"),
        (courier, customer.email, "at_shop"),
        (courier, customer.email, "courier_on_way"),
        (courier, customer.email, "new_message"),
    ]
    for caller, target, type_ in allowed:
        res = await send(client, caller, userId=target, type=type_, order_id=oid)
        assert res.status_code == 200, (caller.email, type_)
    refused = [
        (customer, courier.email, "on_the_way"),  # the customer can't say the courier is on the way
        (courier, customer.email, "order_accepted"),
        (courier, customer.email, "order_cancelled"),
        (customer, parties.stranger.email, "new_message"),  # a stranger through his own order
        (parties.stranger, customer.email, "new_message"),  # not a party
    ]
    for caller, target, type_ in refused:
        res = await send(client, caller, userId=target, type=type_, order_id=oid)
        assert (res.status_code, res.json()) == (403, {"error": "Forbidden"}), (caller.email, type_)
    missing = await send(client, courier, userId=customer.email, type="at_shop", order_id=str(uuid.uuid4()))
    assert (missing.status_code, missing.json()) == (404, {"error": "Order not found"})
    self_missing = await send(client, courier, userId=courier.email, type="at_shop", order_id="nope")
    assert self_missing.status_code == 404
    stored = await rows(Notification, Notification.user_id == customer.id)
    assert {n.type for n in stored} == {"at_shop", "on_the_way", "new_message"}


async def test_courier_with_a_pending_offer_may_send_new_offer(client, parties, factory):
    order = await make_order(parties.customer)
    denied = await send(
        client, parties.courier_user, userId=parties.customer.email, type="new_offer", order_id=str(order.id)
    )
    assert denied.status_code == 403
    await make_offer(order, parties.courier)
    ok = await send(
        client, parties.courier_user, userId=parties.customer.email, type="new_offer", order_id=str(order.id)
    )
    assert ok.status_code == 200
    not_customer = await send(
        client, parties.courier_user, userId=parties.stranger.email, type="new_offer", order_id=str(order.id)
    )
    assert not_customer.status_code == 403
    wrong_type = await send(
        client, parties.courier_user, userId=parties.customer.email, type="at_shop", order_id=str(order.id)
    )
    assert wrong_type.status_code == 403


async def test_courier_profile_id_is_mapped_to_its_user(client, parties):
    by_uuid = await send(client, parties.admin, userId=str(parties.courier.id), type="new_order")
    assert by_uuid.status_code == 200
    async with SessionLocal() as s:
        courier = await s.get(type(parties.courier), parties.courier.id)
        courier.old_import_id = "68d1a2b3c4d5e6f7a8b9c0d1"
        await s.commit()
    by_legacy = await send(client, parties.admin, userId="68d1a2b3c4d5e6f7a8b9c0d1", type="new_order")
    assert by_legacy.status_code == 200
    stored = await rows(Notification, Notification.user_id == parties.courier_user.id)
    assert len(stored) == 2


async def test_push_skipped_reasons_and_push_delivery(client, parties, factory):
    await make_device(parties.customer)
    pushed = await send(client, parties.customer, userId=parties.customer.email, type="new_message")
    assert "push_skipped" not in pushed.json()
    assert len(await rows(PushDelivery, PushDelivery.user_id == parties.customer.id)) == 1

    quiet = await factory.user(notify_chat=False, notify_incoming_orders=False)
    chat = await send(client, quiet, userId=quiet.email, type="message")
    assert chat.json()["push_skipped"] == "chat_messages_disabled"
    incoming = await send(client, quiet, userId=quiet.email, type="incoming_order")
    assert incoming.json()["push_skipped"] == "incoming_orders_disabled"
    assert {n.type for n in await rows(Notification, Notification.user_id == quiet.id)} == {
        "new_message",
        "new_order",
    }
    muted = await factory.user(push_enabled=False)
    off = await send(client, muted, userId=muted.email, type="eta_update")
    assert off.json()["push_skipped"] == "push_disabled"
    # the in-app rows exist anyway
    assert len(await rows(Notification, Notification.user_id.in_([quiet.id, muted.id]))) == 3


def test_preferences_helpers():
    user = User(
        push_enabled=True,
        notify_order_status=False,
        notify_new_orders=True,
        notify_chat=True,
        notify_incoming_orders=True,
    )
    assert push_skip_reason(user, "order_delivered") == "order_status_changes_disabled"
    assert push_skip_reason(user, "emergency_contact") is None
    assert push_skip_reason(None, "new_order") == "push_disabled"
    assert canonical_type("courier_on_way") == "on_the_way"
    with pytest.raises(ValueError):
        canonical_type("nope")
    assert notifications.PREFERENCE_COLUMN["new_message"] == "notify_chat"


async def test_notify_without_push_and_events(session, parties, emitted):
    result = await notify_detailed(
        session, user_id=parties.customer.id, type_="delivered", title_ar="a", title_fr="f", push=False
    )
    assert result.push_skipped is None and result.push is None
    assert {"entity": "Notification", "type": "create", "id": str(result.notification.id)} in emitted
    await session.rollback()


# ─────────────────────────── WhatsApp instead of push ───────────────────────────


@pytest.fixture
async def web_only_customer(parties):
    async with SessionLocal() as s:
        user = await s.get(User, parties.customer.id)
        user.whatsapp_opt_in_at = datetime.now(UTC)
        await s.commit()
    return parties


async def test_whatsapp_fallback_for_on_the_way(session, web_only_customer, http, meta_on):
    p = web_only_customer
    order = await make_order(p.customer, p.courier, status="on_the_way")
    http.responder = lambda _req, _n: ok_wa("wamid.otw")
    result = await notify_detailed(
        session, user_id=p.customer.id, type_="courier_on_way", title_ar="a", title_fr="f", order_id=order.id
    )
    assert result.whatsapp["whatsapp"] == "sent"
    sent = json.loads(http.calls[0].content)
    assert sent["template"]["name"] == "ods_livreur_en_route"
    assert [p["text"] for p in sent["template"]["components"][0]["parameters"]] == ["Sami", "Carrefour"]
    row = (await session.execute(select(OutboundMessage))).scalar_one()
    assert row.idempotency_key == f"ontheway:{order.id}" and row.notification_id == result.notification.id
    # once per order and step
    again = await notify_detailed(
        session, user_id=p.customer.id, type_="on_the_way", title_ar="a", title_fr="f", order_id=order.id
    )
    assert again.whatsapp["duplicate"] is True and len(http.calls) == 1
    await session.rollback()


async def test_whatsapp_fallback_for_new_offer_and_its_guards(session, web_only_customer, http, meta_on):
    p = web_only_customer
    order = await make_order(p.customer)
    await make_offer(order, p.courier, fee="7.25")
    http.responder = lambda _req, _n: ok_wa()
    result = await notify_detailed(
        session, user_id=p.customer.id, type_="new_offer", title_ar="a", title_fr="f", order_id=order.id
    )
    params = json.loads(http.calls[0].content)["template"]["components"][0]["parameters"]
    assert [x["text"] for x in params] == ["Carrefour", "7.250"] and result.whatsapp["whatsapp"] == "sent"
    # no fallback: QA order, other type, someone else's order
    qa = await make_order(p.customer, items_text="QA TEST pain")
    for type_, order_id, user in (("new_offer", qa.id, p.customer), ("at_shop", order.id, p.customer)):
        skipped = await notify_detailed(
            session, user_id=user.id, type_=type_, title_ar="a", title_fr="f", order_id=order_id
        )
        assert skipped.whatsapp is None
    await session.rollback()


async def test_no_whatsapp_when_a_device_exists_or_when_off(session, web_only_customer, http):
    p = web_only_customer
    order = await make_order(p.customer, p.courier, status="on_the_way")
    off = await notify_detailed(
        session, user_id=p.customer.id, type_="on_the_way", title_ar="a", title_fr="f", order_id=order.id
    )
    assert off.whatsapp is None  # WhatsApp not configured
    settings_on = {"WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1"}
    for key, value in settings_on.items():
        setattr(settings, key, value)
    try:
        await make_device(p.customer)
        pushed = await notify_detailed(
            session, user_id=p.customer.id, type_="on_the_way", title_ar="a", title_fr="f", order_id=order.id
        )
        assert pushed.whatsapp is None and pushed.push["attempted"] == 1 and http.calls == []
    finally:
        for key in settings_on:
            setattr(settings, key, "")
    await session.rollback()


async def test_whatsapp_fallback_errors_are_contained(session, web_only_customer, meta_on, monkeypatch):
    p = web_only_customer
    order = await make_order(p.customer, p.courier, status="on_the_way")

    async def broken(*_a, **_k):
        raise RuntimeError("meta down")

    monkeypatch.setattr(notifications.whatsapp, "send_template", broken)
    result = await notify_detailed(
        session, user_id=p.customer.id, type_="on_the_way", title_ar="a", title_fr="f", order_id=order.id
    )
    assert result.whatsapp is None and result.notification.id
    await session.rollback()


# ─────────────────────────── Notification entity ───────────────────────────


async def test_notification_entity_reads_and_is_read_update(client, parties):
    order = await make_order(parties.customer, parties.courier)
    await send(
        client,
        parties.courier_user,
        userId=parties.customer.email,
        type="at_shop",
        order_id=str(order.id),
        metadata={"recipient_role": "customer"},
        body_fr="Au magasin",
    )
    q = {"q": json.dumps({"user_id": parties.customer.email, "is_read": False})}
    [doc] = (await client.get("/api/entities/Notification", params=q, headers=auth(parties.customer))).json()
    assert doc["type"] == "at_shop" and doc["order_id"] == str(order.id) and doc["is_read"] is False
    assert doc["metadata"] == {"recipient_role": "customer"} and doc["body_fr"] == "Au magasin"
    assert set(doc) >= {"title_ar", "title_fr", "body_ar", "user_id", "created_date"}
    assert (await client.get("/api/entities/Notification", headers=auth(parties.stranger))).json() == []
    assert len((await client.get("/api/entities/Notification", headers=auth(parties.admin))).json()) == 1

    url = f"/api/entities/Notification/{doc['id']}"
    stranger = await client.patch(url, json={"is_read": True}, headers=auth(parties.stranger))
    assert stranger.status_code == 404
    admin = await client.patch(url, json={"is_read": True}, headers=auth(parties.admin))
    assert admin.status_code == 403
    bad_id = await client.patch(
        "/api/entities/Notification/nope", json={"is_read": True}, headers=auth(parties.admin)
    )
    assert bad_id.status_code == 404
    read = await client.patch(
        url, json={**doc, "is_read": True, "type": "delivered"}, headers=auth(parties.customer)
    )
    assert read.status_code == 200 and read.json()["is_read"] is True and read.json()["type"] == "at_shop"
    unread = await client.patch(url, json={"is_read": False}, headers=auth(parties.customer))
    assert unread.json()["is_read"] is False
    # a stranger can't delete it (404); its recipient can (Aurora)
    assert (await client.delete(url, headers=auth(parties.stranger))).status_code == 404
    assert (await client.delete(url, headers=auth(parties.customer))).status_code == 200


async def test_notification_entity_create_rules(client, parties):
    await make_device(parties.courier_user)
    admin_payload = {
        "user_id": parties.courier_user.email,
        "type": "account_verified",
        "is_read": False,
        "metadata": {"recipient_role": "courier", "verification_status": "verified"},
        "title_ar": "تم قبول طلبك",
        "title_fr": "Votre demande a été approuvée",
        "body_ar": "…",
        "body_fr": "…",
    }
    created = await client.post("/api/entities/Notification", json=admin_payload, headers=auth(parties.admin))
    assert created.status_code == 201 and created.json()["type"] == "account_verified"
    assert len(await rows(PushDelivery, PushDelivery.user_id == parties.courier_user.id)) == 1
    # the orderFlow fallback into somebody else's list is refused, into one's own accepted
    other = await client.post(
        "/api/entities/Notification",
        json={**admin_payload, "type": "new_message"},
        headers=auth(parties.customer),
    )
    assert other.status_code == 403
    own = await client.post(
        "/api/entities/Notification",
        json={"user_id": parties.customer.email, "type": "message", "title_fr": "x"},
        headers=auth(parties.customer),
    )
    assert own.status_code == 201 and own.json()["type"] == "new_message"
    assert len(await rows(PushDelivery, PushDelivery.user_id == parties.customer.id)) == 0
    bad = [
        ({"type": "new_message"}, "user_id: required"),
        ({"user_id": "ghost@example.test", "type": "new_message"}, "user_id: unknown user"),
        ({"user_id": parties.courier_user.email, "type": "bogus"}, "type: unknown notification type"),
        (
            {"user_id": parties.courier_user.email, "type": "delivered", "order_id": str(uuid.uuid4())},
            "order_id: unknown order",
        ),
        (
            {"user_id": parties.courier_user.email, "type": "delivered", "metadata": {"a": "b" * 2100}},
            "metadata: too large",
        ),
    ]
    for payload, message in bad:
        res = await client.post("/api/entities/Notification", json=payload, headers=auth(parties.admin))
        assert (res.status_code, res.json()["message"]) == (400, message)


# ─────────────────────────── registerDeviceToken / unregisterDeviceToken ───────────────────────────


async def register(client, user, headers=None, **payload):
    return await client.post(
        "/api/functions/registerDeviceToken", json=payload, headers={**auth(user), **(headers or {})}
    )


async def test_register_device_token_validation(client, parties):
    me = parties.customer
    cases = [
        ({}, "Missing token"),
        ({"token": "a b", "platform": "web"}, "Invalid token"),
        ({"token": "x" * 4097, "platform": "web"}, "Invalid token"),
        ({"token": "tok", "platform": "windows"}, "Invalid platform"),
        ({"token": "tok", "platform": "web", "provider": "apns"}, "Invalid provider"),
    ]
    for payload, error in cases:
        res = await register(client, me, **payload)
        assert (res.status_code, res.json()) == (400, {"error": error}), payload


async def test_register_dedupes_moves_and_detects_platform(client, parties, emitted):
    me, other = parties.customer, parties.courier_user
    first = await register(
        client, me, token="fcm-1", platform="WEB", app_version="1" * 40, device_model=7, locale="de"
    )
    assert first.status_code == 200 and first.json()["success"] is True
    again = await register(client, me, token="fcm-1", platform="android", locale="fr")
    assert again.json()["device_token_id"] == first.json()["device_token_id"]
    [row] = await rows(DeviceToken)
    assert (row.platform, row.locale, row.app_version) == ("android", "fr", None)
    # re-login on the same phone with another account: the token moves
    async with SessionLocal() as s:
        stored = await s.get(DeviceToken, row.id)
        stored.is_active, stored.failure_count = False, 3
        await s.commit()
    moved = await register(client, other, token="fcm-1", platform="android")
    assert moved.json()["device_token_id"] == first.json()["device_token_id"]
    [row] = await rows(DeviceToken)
    assert row.user_id == other.id and row.is_active and row.failure_count == 0
    detected = await register(
        client, me, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"}, token="fcm-2"
    )
    assert detected.status_code == 200
    assert {r.token: r.platform for r in await rows(DeviceToken)}["fcm-2"] == "ios"
    assert device_tokens.detect_platform("Linux; Android 14") == "android"
    assert device_tokens.detect_platform(None) == "web"
    assert [e["type"] for e in emitted if e["entity"] == "DeviceToken"] == [
        "create",
        "update",
        "update",
        "create",
    ]


async def test_unregister_device_token(client, parties):
    me = parties.customer
    await make_device(me, token="fcm-a")
    await make_device(me, token="fcm-b")
    await make_device(parties.stranger, token="fcm-c")
    url = "/api/functions/unregisterDeviceToken"
    missing = await client.post(url, json={}, headers=auth(me))
    assert (missing.status_code, missing.json()) == (400, {"error": "Missing token or endpoint_hash"})
    by_token = await client.post(url, json={"token": "fcm-a"}, headers=auth(me))
    assert by_token.json() == {"success": True, "deactivated": 1}
    by_hash = await client.post(
        url, json={"endpoint_hash": device_tokens.endpoint_hash("fcm-b")}, headers=auth(me)
    )
    assert by_hash.json()["deactivated"] == 1
    not_mine = await client.post(url, json={"token": "fcm-c"}, headers=auth(me))
    assert not_mine.json()["deactivated"] == 0
    assert {r.token: r.is_active for r in await rows(DeviceToken)} == {
        "fcm-a": False,
        "fcm-b": False,
        "fcm-c": True,
    }


async def test_device_token_entity(client, parties):
    await make_device(parties.customer, token="fcm-x", locale="ar")
    [doc] = (await client.get("/api/entities/DeviceToken", headers=auth(parties.customer))).json()
    assert (
        doc["endpoint_hash"]
        == hashlib.sha256(b"fcm-x").hexdigest()[:32]
        == device_tokens.endpoint_hash("fcm-x")
    )
    assert doc["provider"] == "fcm" and doc["user_id"] == parties.customer.email and doc["is_active"] is True
    by_hash = await client.get(
        "/api/entities/DeviceToken",
        params={"q": json.dumps({"endpoint_hash": doc["endpoint_hash"]})},
        headers=auth(parties.admin),
    )
    assert len(by_hash.json()) == 1
    assert (await client.get("/api/entities/DeviceToken", headers=auth(parties.stranger))).json() == []
    denied = await client.post(
        "/api/entities/DeviceToken", json={"token": "t"}, headers=auth(parties.customer)
    )
    assert denied.status_code == 403


async def test_message_log_entity_is_admin_only(client, parties):
    async with SessionLocal() as s:
        parent = OutboundMessage(
            channel="whatsapp",
            purpose="customer_no_response",
            to_e164="+21698765432",
            status="failed",
            user_id=parties.customer.id,
            params=["a", "b", "c"],
        )
        s.add(parent)
        await s.flush()
        s.add(
            OutboundMessage(
                channel="sms",
                purpose="customer_no_response",
                to_e164="+21698765432",
                status="sent",
                parent_id=parent.id,
            )
        )
        await s.commit()
    docs = (
        await client.get(
            "/api/entities/MessageLog", params={"sort": "created_date"}, headers=auth(parties.admin)
        )
    ).json()
    by_channel = {d["channel"]: d for d in docs}
    assert by_channel["whatsapp"]["fallback_log_id"] == by_channel["sms"]["id"]
    assert by_channel["sms"]["parent_log_id"] == by_channel["whatsapp"]["id"]
    assert by_channel["whatsapp"]["to"] == "+21698765432" and by_channel["whatsapp"]["params"] == [
        "a",
        "b",
        "c",
    ]
    assert by_channel["whatsapp"]["user_id"] == parties.customer.email
    assert (await client.get("/api/entities/MessageLog", headers=auth(parties.customer))).json() == []


async def test_register_via_service_rejects_nothing_else(session, parties):
    status, body = await device_tokens.register(
        session, to_current_user(parties.customer), {"token": "t1"}, None
    )
    assert status == 200 and uuid.UUID(body["device_token_id"])
    await session.rollback()


async def test_the_recipient_deletes_his_own_notification(client, parties, monkeypatch):
    import app.api.compat_entities as router
    from app.realtime import events

    mine = await notify_detailed_row(parties.customer)
    other = await notify_detailed_row(parties.stranger)
    url = "/api/entities/Notification"
    assert (await client.delete(f"{url}/{other.id}", headers=auth(parties.customer))).status_code == 404
    assert (await client.delete(f"{url}/{mine.id}", headers=auth(parties.admin))).status_code == 403
    assert (await client.delete(f"{url}/not-a-uuid", headers=auth(parties.customer))).status_code == 404
    seen: list[dict] = []
    original = events.emit

    def record(session, entity, type_, id_, audience=None, data=None):
        seen.append({"entity": entity, "type": type_, "id": str(id_), "audience": audience})
        original(session, entity, type_, id_, audience, data)

    monkeypatch.setattr(router, "emit", record)
    res = await client.delete(f"{url}/{mine.id}", headers=auth(parties.customer))
    assert res.status_code == 200, res.text
    assert seen == [
        {"entity": "Notification", "type": "delete", "id": str(mine.id), "audience": [parties.customer.id]}
    ]
    assert await rows(Notification, Notification.id == mine.id) == []
    assert len(await rows(Notification, Notification.id == other.id)) == 1
    assert (await client.delete(f"{url}/{mine.id}", headers=auth(parties.customer))).status_code == 404


async def notify_detailed_row(user):
    async with SessionLocal() as s:
        result = await notify_detailed(
            s, user_id=user.id, type_="new_message", title_ar="a", title_fr="f", push=False
        )
        await s.commit()
        return result.notification


# ─────────────────────────── markNotificationsRead (one call for the page) ───────────────────────────


async def test_mark_notifications_read_in_one_call(client, parties, emitted):
    """QA 06/10 B59: the Notifications page marks its rows read in ONE call, only the caller's
    own unread ones (another user's row, a row already read and junk ids are left alone)."""
    async with SessionLocal() as s:
        mine = [
            Notification(user_id=parties.customer.id, type="delivered", title_ar="t", title_fr="t")
            for _ in range(28)
        ]
        already = Notification(
            user_id=parties.customer.id,
            type="delivered",
            title_ar="t",
            title_fr="t",
            read_at=datetime.now(UTC),
        )
        other = Notification(user_id=parties.stranger.id, type="delivered", title_ar="t", title_fr="t")
        s.add_all([*mine, already, other])
        await s.commit()
        ids = [str(n.id) for n in mine]
        already_id, other_id, already_at = str(already.id), other.id, already.read_at

    res = await client.post(
        "/api/functions/markNotificationsRead",
        json={"ids": [*ids, already_id, str(other_id), "not-an-id"]},
        headers=auth(parties.customer),
    )
    assert (res.status_code, res.json()) == (200, {"success": True, "marked": 28})
    assert all(
        n.read_at is not None for n in await rows(Notification, Notification.user_id == parties.customer.id)
    )
    [kept] = await rows(Notification, Notification.id == uuid.UUID(already_id))
    assert kept.read_at == already_at
    [untouched] = await rows(Notification, Notification.id == other_id)
    assert untouched.read_at is None
    assert sum(1 for e in emitted if e["entity"] == "Notification" and e["type"] == "update") == 28

    again = await client.post(
        "/api/functions/markNotificationsRead", json={"ids": ids}, headers=auth(parties.customer)
    )
    assert again.json() == {"success": True, "marked": 0}
    for bad in ({}, {"ids": []}, {"ids": "x"}, {"ids": [str(uuid.uuid4())] * 501}):
        res = await client.post(
            "/api/functions/markNotificationsRead", json=bad, headers=auth(parties.customer)
        )
        assert res.status_code == 400 and res.json()["error"] == "invalid_ids"
    anonymous = await client.post("/api/functions/markNotificationsRead", json={"ids": ids})
    assert anonymous.status_code == 401
