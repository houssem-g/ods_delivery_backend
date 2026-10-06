"""Blocking and reporting users (App Review 1.2): blockUser / unblockUser / listBlockedUsers /
reportUser / listUserReports / resolveUserReport, and what a block hides (chat, open orders,
offers, dispatch)."""

from datetime import UTC, datetime
from typing import Any

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
        "live_order": {"id": str(order.id), "status": "accepted", "releasable": True},
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


async def test_blocked_list_says_the_role_the_person_was_met_in(client, parties, factory):
    """QA 06/10 B54: a customer who also has a courier profile, blocked by the courier of his
    order, is listed as « client » (with his own name and the order's shop), not « livreur »."""
    await factory.courier(parties.customer, display_name="Zied", phone_e164="+21622000999")
    order = await make_order(parties.customer, parties.courier)
    res = await call(client, "blockUser", parties.courier_user, {"order_id": str(order.id)})
    assert res.status_code == 200
    [row] = (await call(client, "listBlockedUsers", parties.courier_user)).json()["blocked"]
    customer_first = ((await rows(User, User.id == parties.customer.id))[0].full_name or "").split(" ")[0]
    assert row["role"] == "customer" and row["is_courier"] is False
    assert row["first_name"] == (customer_first or "—") and row["first_name"] != "Zied"
    assert row["shop_name"] == "Carrefour" and row["order_date"].endswith("Z")
    # the other way round: the customer blocked the courier of the same order
    await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})
    [row] = (await call(client, "listBlockedUsers", parties.customer)).json()["blocked"]
    assert (row["role"], row["first_name"], row["shop_name"]) == ("courier", "Sami", "Carrefour")


async def test_after_a_block_phones_stay_until_the_running_order_ends(client, parties):
    """Owner's rule (QA 06/10 B55): a block during a running delivery keeps Appeler / WhatsApp
    (the phones) until the order ends; then, and on any order outside a running one, the phones
    between the two people are gone."""
    order = await make_order(parties.customer, parties.courier, contact_phone_e164="+21698765432")

    async def phones() -> tuple[Any, Any, Any]:
        seen_by_customer = await client.get(f"/api/entities/Order/{order.id}", headers=auth(parties.customer))
        seen_by_courier = await client.get(
            f"/api/entities/Order/{order.id}", headers=auth(parties.courier_user)
        )
        card = (await call(client, "getOrderCourier", parties.customer, {"order_id": str(order.id)})).json()
        return (
            seen_by_customer.json().get("courier_phone"),
            seen_by_courier.json().get("customer_phone"),
            card["courier"]["phone"],
        )

    before = await phones()
    assert before == (parties.courier.phone_e164, "+21698765432", parties.courier.phone_e164)
    assert (await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})).status_code == 200
    assert await phones() == before  # the delivery runs: still reachable

    async with SessionLocal() as s:
        row = await s.get(Order, order.id)
        row.status, row.delivered_at = "delivered", datetime.now(UTC)
        await s.commit()
    assert await phones() == (None, None, "")  # over: gone both ways

    # unblocked: the phones of the finished order come back
    await call(client, "unblockUser", parties.customer, {"user_id": str(parties.courier_user.id)})
    assert await phones() == before


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

    # each side is told who blocked: the customer can unblock, the courier was blocked
    for sender, who in ((parties.customer, "me"), (parties.courier_user, "them")):
        res = await call(client, "sendOrderMessage", sender, {"order_id": str(order.id), "content": "hey"})
        assert res.status_code == 403 and res.json() == {"error": "blocked", "blocked_by": who}
        res = await call(client, "getOrderMessages", sender, {"order_id": str(order.id)})
        assert res.status_code == 200 and res.json()["blocked_by"] == who
    res = await call(client, "getOrderMessages", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 200 and res.json()["messages"] == []
    unread = await call(client, "listMyUnreadMessages", parties.customer, {"mode": "fast"})
    assert unread.json()["messages"] == []
    # the admin still sees everything (to judge a report)
    res = await call(client, "getOrderMessages", parties.admin, {"order_id": str(order.id)})
    assert [m["content"] for m in res.json()["messages"]] == ["avant"] and res.json()["blocked_by"] is None

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


# ─────────────────────────── objectionable-word filter ───────────────────────────


def test_text_filter_masks_strong_insults_only():
    from app.services.text_filter import mask

    assert mask("Bonjour, je suis en bas") == ("Bonjour, je suis en bas", False)
    assert mask("espèce de CONNARD !") == ("espèce de ******* !", True)
    assert mask("t'es un c0nnard") == ("t'es un *******", True)
    assert mask("Connaaaard") == ("**********", True)
    assert mask("yezzi ya zebi") == ("yezzi ya ****", True)
    assert mask("ya 9a7ba") == ("ya *****", True)
    assert mask("يا قَحْبَة") == ("يا *******", True)
    # « manyak » in its usual spellings (B53)
    for word in ("manyek", "Manyak", "MANYIK", "manyouk", "mnayek", "mnayak", "manyeeeek", "منياك"):
        assert mask(f"QA TEST {word}") == ("QA TEST " + "*" * len(word), True), word
    # normal words that contain a blocked one are never touched
    for fine in (
        "habite à Tunis",
        "ma3andich",
        "مرحبا، وين وصلت؟",
        "je prends du pain",
        "many thanks",
        "Germany",
        "maniaque",
        "manie",
        "mayonnaise",
        "mnih",
        "",
    ):
        assert mask(fine) == (fine, False)


async def test_chat_and_offer_messages_are_filtered(client, parties):
    order = await make_order(parties.customer, parties.courier)
    res = await call(
        client,
        "sendOrderMessage",
        parties.courier_user,
        {"order_id": str(order.id), "content": "salut connard"},
    )
    assert res.status_code == 200 and res.json()["message"]["content"] == "salut *******"
    open_order = await make_order(parties.customer)
    res = await call(
        client, "createOrderOffer", parties.courier_user,
        {"order_id": str(open_order.id), "fee": 6, "eta_minutes": 20, "message": "j'arrive zebi"},
    )  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["offer"]["message"] == "j'arrive ****"


# ─────────────────────────── blocking the courier of a running order ───────────────────────────


async def test_customer_blocks_his_courier_then_chooses_another_one(client, parties):
    order = await make_order(parties.customer, parties.courier, status="accepted")
    offer = await make_offer(order, parties.courier, status="accepted")
    res = await call(client, "releaseBlockedCourier", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 409 and res.json()["error"] == "not_blocked"

    res = await call(client, "blockUser", parties.customer, {"order_id": str(order.id)})
    assert res.json()["live_order"] == {"id": str(order.id), "status": "accepted", "releasable": True}
    assert (
        await call(client, "releaseBlockedCourier", parties.stranger, {"order_id": str(order.id)})
    ).status_code == 403

    res = await call(client, "releaseBlockedCourier", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 200 and res.json() == {"success": True, "status": "pending"}
    async with SessionLocal() as s:
        fresh = await s.get(Order, order.id)
        assert fresh.status == "pending" and fresh.courier_id is None and fresh.delivery_fee is None
        assert (await s.get(type(offer), offer.id)).status == "expired"
    [note] = await rows(Notification, Notification.user_id == parties.courier_user.id)
    assert note.type == "order_cancelled" and note.title_fr == "❌ Commande retirée"
    # gone for the blocked courier: he no longer sees it, cannot bid
    hidden = await client.get(f"/api/entities/Order/{order.id}", headers=auth(parties.courier_user))
    assert hidden.status_code == 404
    again = await call(client, "releaseBlockedCourier", parties.customer, {"order_id": str(order.id)})
    assert again.status_code == 409 and again.json()["error"] == "no_courier"


async def test_after_the_purchase_the_delivery_goes_on(client, parties):
    order = await make_order(parties.customer, parties.courier, status="purchased")
    res = await call(
        client,
        "reportUser",
        parties.customer,
        {"order_id": str(order.id), "reason": "harassment", "block": True},
    )
    assert res.json()["live_order"] == {"id": str(order.id), "status": "purchased", "releasable": False}
    res = await call(client, "releaseBlockedCourier", parties.customer, {"order_id": str(order.id)})
    assert res.status_code == 409 and res.json()["error"] == "already_purchased"
    # no running order with that person: nothing to decide
    old = await make_order(
        parties.customer, parties.courier, status="delivered", delivered_at=datetime.now(UTC)
    )
    res = await call(client, "blockUser", parties.customer, {"order_id": str(old.id)})
    assert res.json()["live_order"] is None
    # the courier blocking the customer is never asked (only the customer chooses)
    live = await make_order(parties.customer, parties.courier, status="accepted")
    res = await call(client, "blockUser", parties.courier_user, {"order_id": str(live.id)})
    assert res.json()["live_order"] is None
