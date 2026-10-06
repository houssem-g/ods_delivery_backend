"""Order chat: sendOrderMessage, getOrderMessages, markOrderMessagesRead,
listMyUnreadMessages and the Message entity."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Message, Notification
from app.security.deps import to_current_user
from app.services import messages as chat
from tests.factories import auth
from tests.messaging_factories import make_message, make_order


async def call(client, name, user, payload):
    return await client.post(f"/api/functions/{name}", json=payload, headers=auth(user))


async def notifications_of(user):
    async with SessionLocal() as s:
        return list((await s.execute(select(Notification).where(Notification.user_id == user.id))).scalars())


# ─────────────────────────── sendOrderMessage ───────────────────────────


async def test_customer_message_goes_to_the_assigned_courier(client, parties):
    order = await make_order(parties.customer, parties.courier)
    res = await call(
        client, "sendOrderMessage", parties.customer, {"order_id": order.id.hex, "content": "  Salut  "}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True and body["notified"] is True
    msg = body["message"]
    assert msg["content"] == "Salut" and msg["sender_role"] == "customer"
    assert msg["sender_id"] == "client@example.test" and msg["recipient_id"] == "livreur@example.test"
    assert msg["order_id"] == str(order.id) and msg["is_read"] is False and msg["is_template"] is False
    [notif] = await notifications_of(parties.courier_user)
    assert notif.type == "new_message" and notif.order_id == order.id
    assert notif.title_fr == "💬 Message du client" and notif.title_ar == "💬 رسالة من العميل"
    assert notif.body_fr == "Salut"
    assert notif.data["recipient_role"] == "courier" and notif.data["sender_role"] == "customer"
    assert notif.data["message_id"] == msg["id"] and notif.data["message_preview"] == "Salut"


async def test_courier_message_goes_to_the_customer_with_a_preview(client, parties):
    order = await make_order(parties.customer, parties.courier)
    long_text = "x" * 150
    res = await call(
        client, "sendOrderMessage", parties.courier_user, {"order_id": str(order.id), "content": long_text}
    )
    assert res.status_code == 200 and res.json()["message"]["recipient_id"] == "client@example.test"
    [notif] = await notifications_of(parties.customer)
    assert notif.title_fr == "💬 Message du livreur" and notif.title_ar == "💬 رسالة من المندوب"
    assert notif.body_ar == "x" * 80 + "…" and len(notif.data["message_preview"]) == 100
    assert notif.data["recipient_role"] == "customer"


async def test_send_validation_and_refusals(client, parties, factory):
    order = await make_order(parties.customer, parties.courier)
    cases = [
        ({"content": "hi"}, 400, {"error": "Missing order_id"}),
        ({"order_id": str(order.id), "content": "   "}, 400, {"error": "invalid_content", "max": 1000}),
        ({"order_id": str(order.id), "content": "a" * 1001}, 400, {"error": "invalid_content", "max": 1000}),
        ({"order_id": str(order.id), "content": 5}, 400, {"error": "invalid_content", "max": 1000}),
        ({"order_id": "not-a-uuid", "content": "hi"}, 404, {"error": "order_not_found"}),
        ({"order_id": str(uuid.uuid4()), "content": "hi"}, 404, {"error": "order_not_found"}),
    ]
    for payload, status, expected in cases:
        res = await call(client, "sendOrderMessage", parties.customer, payload)
        assert (res.status_code, res.json()) == (status, expected), payload
    stranger = await call(
        client, "sendOrderMessage", parties.stranger, {"order_id": str(order.id), "content": "hi"}
    )
    assert (stranger.status_code, stranger.json()) == (403, {"error": "not_a_party"})
    # an admin is no party of the chat
    admin = await call(
        client, "sendOrderMessage", parties.admin, {"order_id": str(order.id), "content": "hi"}
    )
    assert admin.status_code == 403
    anonymous = await client.post("/api/functions/sendOrderMessage", json={"order_id": str(order.id)})
    assert anonymous.status_code == 401


async def test_finished_order_chat_is_closed_to_both(client, parties):
    """QA 06/10, B47: a cancelled order (and a delivery older than 2 h) closes the chat for both."""
    from datetime import UTC, datetime, timedelta

    order = await make_order(parties.customer, parties.courier, status="cancelled")
    for user in (parties.courier_user, parties.customer):
        r = await call(client, "sendOrderMessage", user, {"order_id": str(order.id), "content": "?"})
        assert (r.status_code, r.json()) == (409, {"error": "order_closed"})
    fresh = await make_order(
        parties.customer, parties.courier, status="delivered", delivered_at=datetime.now(UTC)
    )
    r = await call(
        client, "sendOrderMessage", parties.customer, {"order_id": str(fresh.id), "content": "merci"}
    )
    assert r.status_code == 200
    old = await make_order(
        parties.customer,
        parties.courier,
        status="delivered",
        delivered_at=datetime.now(UTC) - timedelta(hours=3),
    )
    r = await call(client, "sendOrderMessage", parties.customer, {"order_id": str(old.id), "content": "?"})
    assert r.status_code == 409


async def test_bidder_on_an_open_order(client, parties, factory):
    order = await make_order(parties.customer)
    first = await call(
        client, "sendOrderMessage", parties.courier_user, {"order_id": str(order.id), "content": "q?"}
    )
    assert first.status_code == 200
    assert first.json()["message"]["recipient_id"] == "client@example.test"
    # the customer's answer before assignment has no single recipient: nobody notified
    answer = await call(
        client, "sendOrderMessage", parties.customer, {"order_id": str(order.id), "content": "oui"}
    )
    assert answer.json()["notified"] is False and answer.json()["message"]["recipient_id"] == ""
    # an unverified courier is no bidder
    other_user = await factory.user(role="courier")
    await factory.courier(other_user, verification="pending", phone_e164="+21622000111")
    refused = await call(client, "sendOrderMessage", other_user, {"order_id": str(order.id), "content": "q"})
    assert refused.json() == {"error": "not_a_party"}


async def test_bidder_cap_and_burst_limit(client, parties):
    order = await make_order(parties.customer)
    old = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(chat.PRE_ASSIGN_MAX):
        await make_message(order, parties.courier_user, "courier", parties.customer, created_at=old)
    capped = await call(
        client, "sendOrderMessage", parties.courier_user, {"order_id": str(order.id), "content": "q"}
    )
    assert (capped.status_code, capped.json()) == (429, {"error": "too_many_messages"})
    assert "rate limit" not in capped.text.lower()

    assigned = await make_order(parties.customer, parties.courier)
    for _ in range(chat.BURST_MAX):
        await make_message(assigned, parties.customer, "customer", parties.courier_user)
    burst = await call(
        client, "sendOrderMessage", parties.customer, {"order_id": str(assigned.id), "content": "x"}
    )
    assert burst.status_code == 429


async def test_send_emits_message_and_notification_events(session, parties, emitted):
    order = await make_order(parties.customer, parties.courier)
    status, body = await chat.send_order_message(
        session, to_current_user(parties.customer), {"order_id": str(order.id), "content": "hi"}
    )
    assert status == 200
    events = emitted
    assert {"entity": "Message", "type": "create", "id": body["message"]["id"]} in events
    assert any(e["entity"] == "Notification" and e["type"] == "create" for e in events)
    await session.rollback()


async def test_notification_failure_does_not_lose_the_message(session, parties, monkeypatch):
    order = await make_order(parties.customer, parties.courier)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("push down")

    monkeypatch.setattr(chat, "notify", broken)
    status, body = await chat.send_order_message(
        session, to_current_user(parties.customer), {"order_id": str(order.id), "content": "hi"}
    )
    assert status == 200 and body["notified"] is False and body["message"]["content"] == "hi"
    await session.rollback()


# ─────────────────────────── getOrderMessages / markOrderMessagesRead ───────────────────────────


async def test_get_order_messages_and_mark_read(client, parties):
    order = await make_order(parties.customer, parties.courier)
    await make_message(order, parties.customer, "customer", parties.courier_user, body="c1")
    await make_message(order, parties.courier_user, "courier", parties.customer, body="k1")
    plain = await call(client, "getOrderMessages", parties.customer, {"order_id": str(order.id)})
    assert plain.status_code == 200 and "marked" not in plain.json()
    assert [m["content"] for m in plain.json()["messages"]] == ["c1", "k1"]

    marked = await call(
        client, "getOrderMessages", parties.customer, {"order_id": str(order.id), "mark_read": True}
    )
    body = marked.json()
    assert body["marked"] == 1
    assert {m["content"]: m["is_read"] for m in body["messages"]} == {"c1": False, "k1": True}
    # the courier's own side: he marks the customer's message, not his own
    courier = await call(client, "markOrderMessagesRead", parties.courier_user, {"order_id": str(order.id)})
    assert courier.json() == {"success": True, "marked": 1}
    again = await call(client, "markOrderMessagesRead", parties.courier_user, {"order_id": str(order.id)})
    assert again.json()["marked"] == 0


async def test_get_order_messages_refusals_limit_and_admin(client, parties):
    order = await make_order(parties.customer, parties.courier)
    for i in range(5):
        await make_message(order, parties.customer, "customer", parties.courier_user, body=f"m{i}")
    missing = await call(client, "getOrderMessages", parties.customer, {})
    assert (missing.status_code, missing.json()) == (400, {"error": "Missing order_id"})
    unknown = await call(client, "getOrderMessages", parties.customer, {"order_id": str(uuid.uuid4())})
    assert (unknown.status_code, unknown.json()) == (404, {"error": "Order not found"})
    stranger = await call(client, "markOrderMessagesRead", parties.stranger, {"order_id": str(order.id)})
    assert (stranger.status_code, stranger.json()) == (403, {"error": "Forbidden"})
    latest = await call(client, "getOrderMessages", parties.admin, {"order_id": str(order.id), "limit": 2})
    assert [m["content"] for m in latest.json()["messages"]] == ["m3", "m4"]
    junk_limit = await call(
        client, "getOrderMessages", parties.admin, {"order_id": str(order.id), "limit": "x"}
    )
    assert len(junk_limit.json()["messages"]) == 5


async def test_bidder_sees_his_messages_and_the_customers_only(client, parties, factory):
    order = await make_order(parties.customer)
    other_user = await factory.user(role="courier")
    await factory.courier(other_user, verification="verified", phone_e164="+21622000111")
    await make_message(order, parties.courier_user, "courier", parties.customer, body="mine")
    await make_message(order, other_user, "courier", parties.customer, body="theirs")
    await make_message(order, parties.customer, "customer", None, body="answer")
    res = await call(
        client, "getOrderMessages", parties.courier_user, {"order_id": str(order.id), "mark_read": True}
    )
    assert [m["content"] for m in res.json()["messages"]] == ["mine", "answer"]
    assert res.json()["marked"] == 1  # the customer's answer
    customer = await call(client, "getOrderMessages", parties.customer, {"order_id": str(order.id)})
    assert len(customer.json()["messages"]) == 3


# ─────────────────────────── listMyUnreadMessages ───────────────────────────


async def test_unread_fast_mode(client, parties, factory):
    order = await make_order(parties.customer, parties.courier)
    await make_message(order, parties.courier_user, "courier", parties.customer, body="to customer")
    await make_message(order, parties.customer, "customer", parties.courier_user, body="to courier")
    await make_message(order, parties.courier_user, "courier", parties.customer, body="read", read=True)
    # a row pointing at the customer from someone who is not the order's courier
    intruder = await factory.user(role="courier")
    await make_message(order, intruder, "courier", parties.customer, body="intruder")
    # addressed to the customer on an order that is not his
    foreign = await make_order(parties.stranger, parties.courier)
    await make_message(foreign, parties.courier_user, "courier", parties.customer, body="foreign")

    res = await call(client, "listMyUnreadMessages", parties.customer, {"mode": "fast"})
    assert res.json()["mode"] == "fast"
    assert [m["content"] for m in res.json()["messages"]] == ["to customer"]
    courier = await call(client, "listMyUnreadMessages", parties.courier_user, {"mode": "fast"})
    assert [m["content"] for m in courier.json()["messages"]] == ["to courier"]


async def test_unread_full_mode(client, parties):
    assigned = await make_order(parties.customer, parties.courier)
    await make_message(assigned, parties.customer, "customer", None, body="no recipient (old row)")
    open_order = await make_order(parties.customer)
    await make_message(
        open_order, parties.courier_user, "courier", parties.customer, body="question", read=True
    )
    await make_message(open_order, parties.customer, "customer", None, body="answer to bidders")
    closed = await make_order(parties.stranger, status="cancelled")
    await make_message(closed, parties.courier_user, "courier", parties.stranger, body="q2")
    await make_message(closed, parties.stranger, "customer", None, body="closed answer")

    res = await call(client, "listMyUnreadMessages", parties.courier_user, {})
    body = res.json()
    assert body["mode"] == "full"
    assert sorted(m["content"] for m in body["messages"]) == ["answer to bidders", "no recipient (old row)"]
    customer = await call(client, "listMyUnreadMessages", parties.customer, {"mode": "anything"})
    assert customer.json()["messages"] == []
    nobody = await call(client, "listMyUnreadMessages", parties.admin, {})
    assert nobody.json() == {"success": True, "messages": [], "mode": "full"}


# ─────────────────────────── Message entity ───────────────────────────


async def test_message_entity_read_policy_and_no_writes(client, parties, factory):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", parties.courier_user)
    reply = await make_message(order, parties.courier_user, "courier", parties.customer)

    async def visible(user):
        rows = (
            await client.get(
                "/api/entities/Message",
                params={"q": json.dumps({"order_id": str(order.id)})},
                headers=auth(user),
            )
        ).json()
        return sorted(r["id"] for r in rows)

    # Base44 rule: the sender (and admins) only; the other party reads through the functions.
    assert await visible(parties.customer) == [str(msg.id)]
    assert await visible(parties.courier_user) == [str(reply.id)]
    assert await visible(parties.admin) == sorted([str(msg.id), str(reply.id)])
    assert (await client.get("/api/entities/Message", headers=auth(parties.stranger))).json() == []
    for user in (parties.stranger, parties.courier_user):
        one = await client.get(f"/api/entities/Message/{msg.id}", headers=auth(user))
        assert one.status_code == 404
    unread = await client.get(
        "/api/entities/Message",
        params={"q": '{"recipient_id":"livreur@example.test","is_read":false}'},
        headers=auth(parties.courier_user),
    )
    assert unread.json() == []
    create = await client.post(
        "/api/entities/Message",
        json={
            "order_id": str(order.id),
            "sender_id": parties.customer.email,
            "sender_role": "customer",
            "content": "x",
        },
        headers=auth(parties.customer),
    )
    assert create.status_code == 403 and create.json()["error"] == "permission_denied"
    patch = await client.patch(
        f"/api/entities/Message/{msg.id}", json={"content": "y"}, headers=auth(parties.customer)
    )
    assert patch.status_code == 403
    async with SessionLocal() as s:
        assert (await s.get(Message, msg.id)).body == "hello"
