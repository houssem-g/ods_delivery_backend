"""Blocking and reporting users (App Review 1.2): blockUser / unblockUser / listBlockedUsers /
reportUser / listUserReports / resolveUserReport, and what a block hides (chat, open orders,
offers, dispatch)."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, Order, User, UserBlock, UserReport
from app.services import dispatch
from tests.factories import auth
from tests.messaging_factories import make_message, make_offer, make_order


async def call(client, name, user, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def rows(model, *where):
    async with SessionLocal() as s:
        return list((await s.execute(select(model).where(*where))).scalars())


# ─────────────────────────── blockUser / unblockUser / listBlockedUsers ───────────────────────────


async def test_customer_blocks_the_courier_of_his_order(client, parties):
    order = await make_order(parties.customer, parties.courier)
    res = await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "success": True, "blocked_user_id": str(parties.courier_user.id), "already_blocked": False,
    }  # fmt: skip
    again = await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})
    assert again.json()["already_blocked"] is True
    assert len(await rows(UserBlock)) == 1

    listed = (await call(client, "listBlockedUsers", parties.customer)).json()["blocked"]
    assert [(b["user_id"], b["first_name"], b["is_courier"]) for b in listed] == [
        (str(parties.courier_user.id), "Sami", True)
    ]
    assert (await call(client, "listBlockedUsers", parties.courier_user)).json()["blocked"] == []

    res = await call(client, "unblockUser", parties.customer, {"user_id": str(parties.courier_user.id)})
    assert res.status_code == 200
    assert await rows(UserBlock) == []
    res = await call(client, "unblockUser", parties.customer, {"user_id": str(parties.courier_user.id)})
    assert res.status_code == 404


async def test_courier_blocks_the_customer(client, parties):
    order = await make_order(parties.customer, parties.courier)
    res = await call(client, "blockUser", parties.courier_user, {"order_id": str(order.id)})
    assert res.status_code == 200
    assert res.json()["blocked_user_id"] == str(parties.customer.id)


async def test_block_from_a_message(client, parties):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.courier_user, "courier", parties.customer, body="insulte")
    res = await call(client, "blockUser", parties.customer, {"message_id": str(msg.id)})
    assert res.status_code == 200 and res.json()["blocked_user_id"] == str(parties.courier_user.id)
    # the courier cannot "block" the sender of his own message, a stranger cannot use it
    assert (
        await call(client, "blockUser", parties.courier_user, {"message_id": str(msg.id)})
    ).status_code == 403
    assert (await call(client, "blockUser", parties.stranger, {"message_id": str(msg.id)})).status_code == 403


async def test_strangers_cannot_block_through_an_order(client, parties):
    order = await make_order(parties.customer, parties.courier)
    assert (await call(client, "blockUser", parties.stranger, {"order_id": str(order.id)})).status_code == 403
    pending = await make_order(parties.customer)
    res = await call(client, "blockUser", parties.customer, {"order_id": str(pending.id)})
    assert res.status_code == 409 and res.json()["error"] == "no_other_party"
    assert (await call(client, "blockUser", parties.customer, {})).status_code == 400


async def test_customer_blocks_a_bidder_from_his_offer(client, parties):
    order = await make_order(parties.customer)
    await make_offer(order, parties.courier)
    payload = {"order_id": str(order.id), "offer_courier_id": str(parties.courier.id)}
    res = await call(client, "blockUser", parties.customer, payload)
    assert res.status_code == 200 and res.json()["blocked_user_id"] == str(parties.courier_user.id)
    # only the order's customer may use an offer
    assert (await call(client, "blockUser", parties.stranger, payload)).status_code == 403


# ─────────────────────────── what a block hides ───────────────────────────


async def test_block_stops_the_chat_both_ways(client, parties):
    order = await make_order(parties.customer, parties.courier)
    await make_message(order, parties.courier_user, "courier", parties.customer, body="avant")
    await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})

    for sender in (parties.customer, parties.courier_user):
        res = await call(client, "sendOrderMessage", sender, {"order_id": str(order.id), "content": "hey"})
        assert res.status_code == 403 and res.json()["error"] == "blocked"
    res = await call(client, "getOrderMessages", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 200 and res.json()["messages"] == []
    unread = await call(client, "listMyUnreadMessages", parties.customer, {"mode": "fast"})
    assert unread.json()["messages"] == []
    # the admin still sees everything (to judge a report)
    res = await call(client, "getOrderMessages", parties.admin, {"order_id": str(order.id)})
    assert [m["content"] for m in res.json()["messages"]] == ["avant"]

    await call(client, "unblockUser", parties.customer, {"user_id": str(parties.courier_user.id)})
    res = await call(
        client, "sendOrderMessage", parties.courier_user, {"order_id": str(order.id), "content": "ok"}
    )
    assert res.status_code == 200


async def test_blocked_courier_no_longer_sees_or_bids_on_the_customers_orders(client, parties):
    old = await make_order(
        parties.customer, parties.courier, status="delivered", delivered_at=datetime.now(UTC)
    )
    await call(client, "blockUser", parties.customer, {"order_id": str(old.id)})
    open_order = await make_order(parties.customer)

    other = await make_order(parties.stranger)
    seen = await client.get(f"/api/entities/Order/{other.id}", headers=auth(parties.courier_user))
    assert seen.status_code == 200  # an open order of someone else: visible to bid
    hidden = await client.get(f"/api/entities/Order/{open_order.id}", headers=auth(parties.courier_user))
    assert hidden.status_code == 404
    res = await call(
        client, "createOrderOffer", parties.courier_user,
        {"order_id": str(open_order.id), "courier_id": str(parties.courier.id), "fee": 6},
    )  # fmt: skip
    assert res.status_code == 409 and res.json()["error"] == "blocked"


async def test_customer_no_longer_sees_or_accepts_a_blocked_couriers_offer(client, parties):
    order = await make_order(parties.customer)
    offer = await make_offer(order, parties.courier)
    await call(
        client, "blockUser", parties.customer,
        {"order_id": str(order.id), "offer_courier_id": str(parties.courier.id)},
    )  # fmt: skip
    hidden = await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(parties.customer))
    assert hidden.status_code == 404
    own = await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(parties.courier_user))
    assert own.status_code == 200  # the courier still sees his own offer
    res = await call(
        client, "acceptOrderOffer", parties.customer, {"order_id": str(order.id), "offer_id": str(offer.id)}
    )
    assert res.status_code == 409 and res.json()["error"] == "blocked"


async def test_dispatch_skips_blocked_couriers(parties, monkeypatch):
    from app.services import safety

    async with SessionLocal() as s:
        s.add(UserBlock(blocker_id=parties.customer.id, blocked_id=parties.courier_user.id))
        await s.commit()
    async with SessionLocal() as s:
        order = await make_order(parties.customer)
        found = await safety.blocked_with(s, order.customer_id)
        assert found == {parties.courier_user.id}
    assert dispatch.blocked_with is safety.blocked_with


# ─────────────────────────── reportUser + admin ───────────────────────────


async def test_report_a_message_notifies_admins_and_can_block(client, parties):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.courier_user, "courier", parties.customer, body="tu vas voir")
    res = await call(
        client, "reportUser", parties.customer,
        {"message_id": str(msg.id), "reason": "harassment", "details": "menace", "block": True},
    )  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["blocked"] is True
    [report] = await rows(UserReport)
    assert report.reported_id == parties.courier_user.id and report.message_excerpt == "tu vas voir"
    assert report.reason == "harassment" and report.details == "menace" and report.status == "open"
    assert len(await rows(UserBlock)) == 1
    [note] = await rows(Notification, Notification.type == "user_reported")
    assert note.user_id == parties.admin.id and "Harcèlement" in note.body_fr

    assert (
        await call(client, "reportUser", parties.customer, {"order_id": str(order.id), "reason": "x"})
    ).status_code == 400
    assert (await call(client, "listUserReports", parties.customer)).status_code == 403

    listed = (await call(client, "listUserReports", parties.admin)).json()["reports"]
    assert len(listed) == 1 and listed[0]["reported"]["email"] == "livreur@example.test"
    assert listed[0]["reported"]["reports_total"] == 1 and listed[0]["reported"]["is_active"] is True

    res = await call(
        client, "resolveUserReport", parties.admin,
        {"report_id": listed[0]["id"], "status": "resolved", "disable_user": True},
    )  # fmt: skip
    assert res.json() == {"success": True, "status": "resolved", "user_disabled": True}
    async with SessionLocal() as s:
        assert (await s.get(User, parties.courier_user.id)).disabled_at is not None
    assert (await call(client, "listUserReports", parties.admin)).json()["reports"] == []
    assert (
        len((await call(client, "listUserReports", parties.admin, {"status": "all"})).json()["reports"]) == 1
    )


async def test_courier_reports_the_customer_without_blocking(client, parties):
    order = await make_order(parties.customer, parties.courier)
    res = await call(
        client, "reportUser", parties.courier_user, {"order_id": str(order.id), "reason": "fraud"}
    )
    assert res.status_code == 200 and res.json()["blocked"] is False
    [report] = await rows(UserReport)
    assert report.reported_id == parties.customer.id and report.message_id is None
    assert await rows(UserBlock) == []
    async with SessionLocal() as s:
        assert (await s.get(Order, order.id)).status == "accepted"
