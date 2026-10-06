"""Aurora chat: photos and voice notes (private uploads), read receipts, the typing signal and
the "new message" ping, the hub's signal delivery, the chat upload types."""

import uuid
from typing import Any

import pytest

from app.db import SessionLocal
from app.models import File, Message, Notification
from app.realtime.hub import Hub
from app.security.deps import CurrentUser
from app.services import messages as chat
from app.storage import keys
from tests.factories import auth
from tests.messaging_factories import make_message, make_order


async def call(client, name, user, payload):
    return await client.post(f"/api/functions/{name}", json=payload, headers=auth(user))


async def private_file(owner, *, purpose="chat", content_type="image/jpeg", size=1000, ext="jpg") -> str:
    key = f"private/{purpose}/{owner.id}/{uuid.uuid4().hex}.{ext}"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key, owner_id=owner.id, visibility="private", purpose=purpose,
                content_type=content_type, size_bytes=size,
            )
        )  # fmt: skip
        await s.commit()
    return key


async def rows(model, *where):
    from sqlalchemy import select

    async with SessionLocal() as s:
        return list((await s.execute(select(model).where(*where))).scalars())


# ─────────────────────────── attachments ───────────────────────────


async def test_photo_after_the_burst_limit_is_refused_not_lost(client, parties):
    """QA 06/10 B52: a photo right after « Trop de messages » answers 429 too_many_messages (the
    app says so) and stores nothing, never a success with a message that never shows."""
    order = await make_order(parties.customer, parties.courier)
    for _ in range(chat.BURST_MAX):
        await make_message(order, parties.customer, "customer", parties.courier_user)
    key = await private_file(parties.customer)
    res = await call(
        client, "sendOrderMessage", parties.customer,
        {"order_id": str(order.id), "content": "", "attachment_url": key, "attachment_type": "image"},
    )  # fmt: skip
    assert (res.status_code, res.json()) == (429, {"error": "too_many_messages"})
    assert await rows(Message, Message.order_id == order.id, Message.attachment_key == key) == []


async def test_photo_message_without_text(client, parties, emitted):
    order = await make_order(parties.customer, parties.courier)
    key = await private_file(parties.courier_user)
    res = await call(
        client, "sendOrderMessage", parties.courier_user,
        {"order_id": str(order.id), "content": "", "attachment_url": key, "attachment_type": "image"},
    )  # fmt: skip
    assert res.status_code == 200, res.text
    msg = res.json()["message"]
    assert msg["content"] == "" and msg["attachment_type"] == "image" and msg["attachment_duration"] is None
    assert key in msg["attachment_url"] and "X-Amz-Signature" in msg["attachment_url"]
    assert msg["read_at"] is None
    [note] = await rows(Notification, Notification.user_id == parties.customer.id)
    assert note.body_fr == "📷 Photo" and note.body_ar == "📷 صورة"
    assert note.data["attachment_type"] == "image"
    ping = [e for e in emitted if e["type"] == "signal"]
    assert ping == [
        {
            "entity": "Message",
            "type": "signal",
            "id": str(order.id),
            "audience": [str(parties.customer.id)],
            "data": {
                "kind": "message",
                "order_id": str(order.id),
                "message_id": msg["id"],
                "sender_role": "courier",
            },
        }
    ]


async def test_voice_note_with_duration_and_text(client, parties):
    order = await make_order(parties.customer, parties.courier)
    key = await private_file(parties.customer, content_type="audio/webm", size=200_000, ext="webm")
    res = await call(
        client, "sendOrderMessage", parties.customer,
        {"order_id": str(order.id), "content": "écoute", "attachment_url": key, "attachment_duration": 12.4},
    )  # fmt: skip
    assert res.status_code == 200, res.text
    msg = res.json()["message"]
    assert msg["attachment_type"] == "audio" and msg["attachment_duration"] == 12
    [note] = await rows(Notification, Notification.user_id == parties.courier_user.id)
    assert note.body_fr == "🎤 Message vocal · écoute" and note.body_ar == "🎤 رسالة صوتية · écoute"


async def test_attachment_refusals(client, parties, factory):
    order = await make_order(parties.customer, parties.courier)
    base = {"order_id": str(order.id), "content": ""}
    someone_elses = await private_file(parties.stranger)
    id_document = await private_file(parties.courier_user, purpose="courier_id")
    public_key = f"public/chat/{uuid.uuid4().hex}.jpg"
    pdf = await private_file(parties.courier_user, content_type="application/pdf", ext="pdf")
    big_audio = await private_file(
        parties.courier_user, content_type="audio/ogg", size=keys.CHAT_AUDIO_MAX_BYTES + 1, ext="ogg"
    )
    big_image = await private_file(parties.courier_user, size=keys.CHAT_IMAGE_MAX_BYTES + 1)
    photo = await private_file(parties.courier_user)
    audio = await private_file(parties.courier_user, content_type="audio/mp4", ext="m4a")
    cases = [
        ({}, "invalid_content"),
        ({"attachment_url": "private/chat/nope.jpg"}, "invalid_attachment"),
        ({"attachment_url": someone_elses}, "invalid_attachment"),
        ({"attachment_url": id_document}, "invalid_attachment"),  # never another purpose's file
        ({"attachment_url": public_key}, "invalid_attachment"),
        ({"attachment_url": 12}, "invalid_attachment"),
        ({"attachment_url": pdf}, "invalid_attachment"),
        ({"attachment_url": photo, "attachment_type": "audio"}, "invalid_attachment"),
        ({"attachment_url": big_audio}, "attachment_too_large"),
        ({"attachment_url": big_image}, "attachment_too_large"),
        ({"attachment_url": audio, "attachment_duration": 601}, "invalid_attachment_duration"),
        ({"attachment_url": audio, "attachment_duration": "x"}, "invalid_attachment_duration"),
    ]
    for extra, error in cases:
        res = await call(client, "sendOrderMessage", parties.courier_user, {**base, **extra})
        assert res.status_code == 400 and res.json()["error"] == error, extra
    assert await rows(Message) == []
    # a stranger can't post, attachment or not
    mine = await private_file(parties.stranger)
    res = await call(client, "sendOrderMessage", parties.stranger, {**base, "attachment_url": mine})
    assert res.status_code == 403


async def test_messages_list_signs_attachments_and_shows_read_receipts(client, parties):
    order = await make_order(parties.customer, parties.courier)
    key = await private_file(parties.customer)
    sent = await call(
        client,
        "sendOrderMessage",
        parties.customer,
        {"order_id": str(order.id), "content": "", "attachment_url": key},
    )
    assert sent.status_code == 200
    await make_message(order, parties.courier_user, "courier", parties.customer, body="ok")
    # the courier reads (and marks) the chat
    seen = await call(
        client, "getOrderMessages", parties.courier_user, {"order_id": str(order.id), "mark_read": True}
    )
    first = seen.json()["messages"][0]
    assert first["attachment_type"] == "image" and key in first["attachment_url"]
    assert first["read_at"] is None  # not the courier's own message
    mine = await call(client, "getOrderMessages", parties.customer, {"order_id": str(order.id)})
    photo, reply = mine.json()["messages"]
    assert photo["read_at"] is not None and photo["read_at"].endswith("Z")  # "Lu 18:39 ✓✓"
    assert reply["read_at"] is None and reply["attachment_url"] is None
    # the admin (reading only) sees it too; a stranger nothing
    admin = await call(client, "getOrderMessages", parties.admin, {"order_id": str(order.id)})
    assert admin.json()["messages"][0]["attachment_url"] is not None
    assert (
        await call(client, "getOrderMessages", parties.stranger, {"order_id": str(order.id)})
    ).status_code == 403
    # the unread list gives the type (preview), never a URL
    await make_message(order, parties.courier_user, "courier", parties.customer, body="encore")
    unread = await call(client, "listMyUnreadMessages", parties.customer, {})
    assert all(m["attachment_url"] is None for m in unread.json()["messages"])


async def test_chat_upload_accepts_voice_notes_only_for_chat(client, factory, monkeypatch):
    stored: list[str] = []

    async def fake_put(key: str, body: bytes, content_type: str) -> None:
        stored.append(key)

    monkeypatch.setattr(chat.s3, "put_object", fake_put)
    user = await factory.user()
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 64

    async def upload(body, content_type, purpose, visibility="private"):
        return await client.post(
            "/api/files/upload",
            files={"file": ("note", body, content_type)},
            data={"visibility": visibility, "purpose": purpose},
            headers=auth(user),
        )

    ok = await upload(webm, "audio/webm;codecs=opus", "chat")
    assert ok.status_code == 200, ok.text
    assert ok.json()["file_uri"].startswith(f"private/chat/{user.id}/") and ok.json()["file_uri"].endswith(
        ".webm"
    )
    assert (await upload(webm, "audio/webm", "receipt")).status_code == 415
    assert (await upload(webm, "audio/webm", "chat", visibility="public")).status_code == 415
    assert (await upload(b"not audio" * 8, "audio/ogg", "chat")).status_code == 415
    too_big = await upload(b"OggS" + b"\x00" * keys.CHAT_AUDIO_MAX_BYTES, "audio/ogg", "chat")
    assert too_big.status_code == 413
    assert len(stored) == 1


def test_audio_sniffing():
    assert keys.sniff_matches("audio/ogg", b"OggS\x00")
    assert keys.sniff_matches("audio/mp4", b"\x00\x00\x00\x18ftypM4A ")
    assert keys.sniff_matches("audio/x-m4a", b"\x00\x00\x00\x18ftypM4A ")
    assert keys.sniff_matches("audio/aac", b"\xff\xf1\x50\x80")
    assert keys.sniff_matches("audio/aac", b"ADIF")
    assert keys.sniff_matches("audio/mpeg", b"ID3\x04")
    assert keys.sniff_matches("audio/mpeg", b"\xff\xfb\x90\x00")
    assert keys.sniff_matches("audio/wav", b"RIFF\x00\x00\x00\x00WAVEfmt ")
    assert not keys.sniff_matches("audio/wav", b"RIFF\x00\x00\x00\x00WEBP")
    assert not keys.sniff_matches("audio/aac", b"<svg")
    assert keys.allowed_types("private", "chat")["audio/mp4"] == "m4a"
    assert "audio/mp4" not in keys.allowed_types("private", "receipt")
    assert "courier_doc" in keys.ADMIN_SIGNED_PURPOSES


async def test_courier_documents_are_never_signed_by_the_files_route(client, factory):
    owner = await factory.user()
    key = await private_file(owner, purpose="courier_doc")
    res = await client.post("/api/files/signed-url", json={"file_uri": key}, headers=auth(owner))
    assert res.status_code == 403


# ─────────────────────────── typing ───────────────────────────


async def test_typing_goes_to_the_other_party_only(client, parties, emitted):
    order = await make_order(parties.customer, parties.courier, status="on_the_way")
    res = await call(client, "signalTyping", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 200 and res.json() == {"success": True}
    res = await call(client, "signalTyping", parties.courier_user, {"order_id": str(order.id)})
    assert res.status_code == 200
    signals = [e for e in emitted if e["type"] == "signal"]
    base = {"entity": "Message", "type": "signal", "id": str(order.id)}
    assert signals == [
        {
            **base,
            "audience": [str(parties.courier_user.id)],
            "data": {"kind": "typing", "order_id": str(order.id), "sender_role": "customer"},
        },
        {
            **base,
            "audience": [str(parties.customer.id)],
            "data": {"kind": "typing", "order_id": str(order.id), "sender_role": "courier"},
        },
    ]


async def test_typing_refusals(client, parties):
    live = await make_order(parties.customer, parties.courier, status="at_shop")
    open_order = await make_order(parties.customer)
    done = await make_order(parties.customer, parties.courier, status="cancelled")
    cases = [
        (parties.customer, {}, 400, "Missing order_id"),
        (parties.customer, {"order_id": str(uuid.uuid4())}, 404, "order_not_found"),
        (parties.stranger, {"order_id": str(live.id)}, 403, "not_a_party"),
        (parties.admin, {"order_id": str(live.id)}, 403, "not_a_party"),
        (parties.courier_user, {"order_id": str(open_order.id)}, 403, "not_a_party"),  # a bidder
        (parties.customer, {"order_id": str(open_order.id)}, 409, "order_not_live"),
        (parties.customer, {"order_id": str(done.id)}, 409, "order_not_live"),
    ]
    for user, payload, status, error in cases:
        res = await call(client, "signalTyping", user, payload)
        assert (res.status_code, res.json()["error"]) == (status, error), payload
    for _ in range(16):  # 20 a minute, the customer's 4 refused calls above included
        assert (
            await call(client, "signalTyping", parties.customer, {"order_id": str(live.id)})
        ).status_code == 200
    res = await call(client, "signalTyping", parties.customer, {"order_id": str(live.id)})
    assert res.status_code == 429 and res.json()["error"] == "too_many_typing_signals"


# ─────────────────────────── hub ───────────────────────────


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        pass


def _user(role: str = "customer") -> CurrentUser:
    return CurrentUser(id=uuid.uuid4(), email="u@example.test", role=role, full_name="")


async def test_hub_forwards_signals_to_their_audience_only():
    hub = Hub()
    target, other, admin = _user(), _user(), _user("admin")
    sockets = {u.id: FakeSocket() for u in (target, other, admin)}
    for u in (target, other, admin):
        hub.add(sockets[u.id], u).entities.add("Message")
    unsubscribed = FakeSocket()
    hub.add(unsubscribed, target)  # the same user, not subscribed to Message
    data = {"kind": "typing", "order_id": "o-1", "sender_role": "courier"}
    await hub.deliver(
        {"entity": "Message", "type": "signal", "id": "o-1", "audience": [str(target.id)], "data": data}
    )
    assert [len(sockets[u.id].sent) for u in (target, other, admin)] == [1, 0, 0]  # not even admins
    assert unsubscribed.sent == []
    [message] = sockets[target.id].sent
    assert message["entity"] == "Message" and message["type"] == "signal" and message["id"] == "o-1"
    assert message["data"] == data and "timestamp" in message
    await hub.deliver({"entity": "Message", "type": "signal", "id": "o-1"})  # no audience: nobody
    assert len(sockets[target.id].sent) == 1


async def test_emit_refuses_a_signal_without_audience(session):
    from app.realtime import events

    with pytest.raises(ValueError):
        events.emit(session, "Message", "signal", "o-1", data={"kind": "typing"})
    events.emit(session, "Message", "signal", "o-1", audience=["u"], data={"kind": "typing"})
    assert events.pending_events(session) == [
        {"entity": "Message", "type": "signal", "id": "o-1", "audience": ["u"], "data": {"kind": "typing"}}
    ]
    await session.rollback()
