"""QA campaign 18-19/10/2026, the minor and cosmetic findings fixed server side (m1 … m54, c1 … c6 of
the rapport technique) and the 4 « petits défauts » seen in prod (P1 … P4). One test per fix."""

from typing import Any

import pytest

from app.services import order_texts
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def order_doc(client, user, order) -> dict[str, Any]:
    r = await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


# --- m7: Arabic minutes agree with the number, « حوالي » instead of « ~ » / « ≈ … دق » -----------------


def test_m7_arabic_minutes_in_the_order_notices():
    road = order_texts.on_the_way(5, "Monoprix", None)
    assert "حوالي 5 دقائق" in road["body_ar"] and "~" not in road["body_ar"]
    offer = order_texts.new_offer_for_customer("Karim Trabelsi", 4, 15)
    assert offer["body_ar"].endswith("الوصول إليك حوالي 15 دقيقة") and "دق " not in offer["body_ar"] + " "
    assert order_texts.new_offer_for_customer("Karim", 4, 2)["body_ar"].endswith("حوالي دقيقتين")


# --- m14: « Offre acceptée » of a Vitrine order: no « QA TEST [Vitrine] … — 3.200 DT » -------------------


def test_m14_offer_accepted_notice_is_clean():
    items = (
        "QA TEST [Vitrine] Coffee and Tea\n1× Espresso — 3.200 DT\n2× Croissant — 5.000 DT · Sans sucre\n"
        "Total articles : 8.200 DT"
    )
    text = order_texts.offer_accepted_for_courier("QA TEST Coffee and Tea", items)
    for body in (text["body_fr"], text["body_ar"]):
        assert "QA TEST" not in body and "[Vitrine]" not in body and "DT" not in body and "Total" not in body
    assert text["body_fr"] == "Coffee and Tea : 2 articles. Allez au magasin."
    assert text["body_ar"] == "Coffee and Tea: منتجان. توجّه إلى المتجر."
    one = order_texts.offer_accepted_for_courier("Monoprix", "2x Pain")
    assert one["body_fr"] == "Monoprix : 2x Pain. Allez au magasin."


# --- m43: « Rien n'est disponible » is never the FR / AR pair glued together ------------------------------


async def test_m43_nothing_available_without_the_glued_text(client, world):
    from tests.test_stock_checks import at_shop, report

    order = await at_shop(world)
    r = await report(client, world, order, missing_text="", nothing_available=True)
    assert r.status_code == 200, r.text
    assert r.json()["stock_check"]["missing_text"] == ""
    doc = await order_doc(client, world.customer, order)
    assert doc["stock_check"]["missing_text"] == "" and doc["stock_check"]["nothing_available"] is True
    [alert] = await notifications(world.customer, "stock_check")
    assert "/" not in alert.body_fr and "Rien n'est" not in alert.body_ar and "لا يتوفر" not in alert.body_fr
    assert alert.body_fr.startswith("Le livreur ne trouve pas votre commande. Répondez")
    # the courier's own words, when he wrote some, are kept
    assert (
        "(rayon vide)" in order_texts.stock_check_for_customer("rayon vide", None, None, True, 3)["body_fr"]
    )


# --- c3: a typed delay outside 1-600 min is refused, never silently replaced by the computation -----------


async def test_c3_offer_with_an_impossible_delay_is_refused(client, world):
    order = await world.order()
    for eta in (0, 601, "abc"):
        r = await call(
            client,
            world.courier_user,
            "createOrderOffer",
            {"order_id": str(order.id), "fee": 5, "eta_minutes": eta},
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_eta", (eta, r.text)
    ok = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert ok.status_code == 200, ok.text  # no delay at all: still the computed one (older apps)
    offer_id = ok.json()["offer"]["id"] if "offer" in ok.json() else ok.json()["id"]
    edit = await call(
        client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 6, "eta_minutes": 0}
    )
    assert edit.status_code == 400 and edit.json()["error"] == "invalid_eta"


# --- c4: « le − cher » on every offer at the lowest price, on none when all prices are equal --------------


async def test_c4_cheapest_badge_on_every_tied_offer(client, world, factory):
    from tests.test_offer_rank import rivals

    order = await world.order(status="offers_received")
    await rivals(world, factory, order, "4", "4")
    body = (
        await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 6})
    ).json()
    assert sorted((e["fee"], e["cheapest"]) for e in body["board"]) == [
        (4.0, True),
        (4.0, True),
        (6.0, False),
    ]
    same = (
        await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 4})
    ).json()
    assert [e["cheapest"] for e in same["board"]] == [False, False, False]


# --- m48: a block rejects the pending offers between the two; no rank on a blocked order ----------------


async def test_m48_block_rejects_the_offer_and_the_rank(client, world):
    order = await world.order(status="offers_received")
    offer = await world.offer(order, fee="5")
    r = await call(
        client,
        world.customer,
        "blockUser",
        {"order_id": str(order.id), "offer_courier_id": str(world.courier.id)},
    )
    assert r.status_code == 200, r.text
    from app.models import Order as OrderRow
    from app.models import OrderOffer
    from tests.order_helpers import reload

    assert (await reload(OrderOffer, offer.id)).status == "rejected"
    assert (await reload(OrderRow, order.id)).status == "pending"  # no pending offer left
    rank = await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 5})
    assert rank.status_code in (404, 409)  # hidden from him, or 409 blocked — never « 1ʳᵉ sur 2 »
    batch = await call(client, world.courier_user, "getOfferRank", {"order_ids": [str(order.id)]})
    assert batch.json()["ranks"] == {}
    mine = await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))
    assert mine.json()["closed_reason"] == "blocked"


async def test_m48_rank_refused_while_blocked(client, world):
    """The order still visible to him (the customer blocked by the courier side): 409 blocked."""
    from app.services import offers as offers_service

    order = await world.order(status="offers_received")
    await world.offer(order, fee="5")
    async with __import__("app.db", fromlist=["SessionLocal"]).SessionLocal() as s:
        from app.models import UserBlock

        s.add(UserBlock(blocker_id=world.courier_user.id, blocked_id=world.customer.id))
        await s.commit()
    assert offers_service  # the read rule may hide the order: both answers keep the rank away
    r = await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 409 and r.json()["error"] == "blocked"


# --- m53: why an offer closed — the customer cancelled ≠ the customer chose someone else ------------------


async def test_m53_closed_reason_of_an_offer(client, world, factory):
    from tests.test_offer_rank import rivals

    cancelled = await world.order(status="offers_received")
    mine = await world.offer(cancelled, fee="5")
    r = await call(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": str(cancelled.id), "cancelled_by": "customer", "reason": "changed_mind"},
    )
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/api/entities/OrderOffer/{mine.id}", headers=auth(world.courier_user))).json()
    assert doc["status"] == "rejected" and doc["closed_reason"] == "order_cancelled"

    chosen = await world.order(status="offers_received")
    lost = await world.offer(chosen, fee="5")
    [winner] = await rivals(world, factory, chosen, "4")
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(chosen.id), "offer_id": str(winner.id)}
    )
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/api/entities/OrderOffer/{lost.id}", headers=auth(world.courier_user))).json()
    assert doc["status"] == "rejected" and doc["closed_reason"] == "not_retained"
    pending = await world.offer(await world.order(status="offers_received"), fee="5")
    doc = (
        await client.get(f"/api/entities/OrderOffer/{pending.id}", headers=auth(world.courier_user))
    ).json()
    assert doc["closed_reason"] is None


# --- m39 / m49: no e-mail of the other side, no preferred_courier_id for the other couriers --------------


async def test_m39_m49_no_emails_nor_referral_for_strangers(client, world, factory):
    other_user = await factory.user(email="other-courier@example.test", full_name="Sami", profile=False)
    other = await world.make_courier(other_user, display_name="Sami")
    order = await world.order(status="offers_received", preferred_courier_id=world.courier.id)
    offer = await world.offer(order, fee="5")

    bidder = await order_doc(client, other_user, order)  # an open order another courier may bid on
    assert bidder["customer_id"] is None and bidder["created_by"] is None and bidder["customer_name"] is None
    assert bidder["preferred_courier_id"] is None  # m49
    his = await order_doc(client, world.courier_user, order)  # the referred courier keeps his badge
    assert his["preferred_courier_id"] == str(world.courier.id) and his["customer_id"] is None
    mine = await order_doc(client, world.customer, order)
    assert mine["customer_id"] == "cust@example.test" and mine["created_by"] == "cust@example.test"
    assert mine["preferred_courier_id"] == str(world.courier.id)

    seen_by_customer = (
        await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.customer))
    ).json()
    assert seen_by_customer["courier_user_id"] is None and seen_by_customer["created_by"] is None
    seen_by_courier = (
        await client.get(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))
    ).json()
    assert (
        seen_by_courier["customer_id"] is None
        and seen_by_courier["courier_user_id"] == "courier@example.test"
    )
    # a hidden field cannot be probed by a filter either
    probe = await client.get(
        "/api/entities/Order", params={"q": '{"customer_id": "cust@example.test"}'}, headers=auth(other_user)
    )
    assert probe.status_code == 200 and probe.json() == []

    # acceptOrderOffer: the courier's profile id, not his e-mail
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(offer.id)}
    )
    assert r.status_code == 200 and r.json()["courier_user_id"] == str(world.courier.id)
    accepted = await order_doc(client, world.customer, order)
    assert accepted["courier_user_id"] is None  # the customer never reads the courier's e-mail
    assert (await order_doc(client, world.courier_user, order))["customer_id"] == "cust@example.test"  # party
    assert other  # (the other courier no longer reads the order once it is taken)


async def test_m39_app_settings_editor_for_admins_only(client, world):
    from sqlalchemy import text

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO app_settings (key, value, updated_by) VALUES ('main', '{}'::jsonb, :a) "
                "ON CONFLICT (key) DO UPDATE SET updated_by = :a"
            ),
            {"a": world.admin.id},
        )
        await s.commit()
    seen = (await client.get("/api/entities/AppSettings/main", headers=auth(world.customer))).json()
    assert seen["updated_by"] is None
    admin = (await client.get("/api/entities/AppSettings/main", headers=auth(world.admin))).json()
    assert admin["updated_by"] == "admin@example.test"


# --- m38: the couriers the customer did not choose are told, in the app only ------------------------------


async def test_m38_not_retained_couriers_are_told_without_a_push(client, world, factory):
    from tests.order_helpers import device, pushes
    from tests.test_offer_rank import rivals

    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="5")
    [loser] = await rivals(world, factory, order, "6")
    from app.models import Courier
    from tests.order_helpers import reload

    loser_user_id = (await reload(Courier, loser.courier_id)).user_id
    from app.models import User

    loser_user = await reload(User, loser_user_id)
    await device(loser_user)
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(mine.id)}
    )
    assert r.status_code == 200, r.text
    [note] = await notifications(loser_user, "offer_not_retained")
    assert note.title_fr == "Offre non retenue" and "autre offre" in note.body_fr and note.body_ar
    assert note.order_id == order.id and note.data["recipient_role"] == "courier"
    assert await pushes(loser_user) == []  # nothing for him to do: no push
    assert await notifications(world.courier_user, "offer_not_retained") == []  # the chosen one


# --- m52: « Livreur en route », « Changements d'heure », « Confirmation de livraison » really apply --------


async def test_m52_detailed_switches_are_stored_and_applied(client, world):
    from app.db import SessionLocal
    from app.models import Order as OrderRow
    from app.services import step_notices
    from tests.order_helpers import device, pushes

    r = await client.patch(
        f"/api/entities/UserProfile/{world.customer.id}",
        json={"notification_preferences": {"delivery_updates": False, "delivery_confirmations": False}},
        headers=auth(world.customer),
    )
    assert r.status_code == 200, r.text
    prefs = r.json()["notification_preferences"]
    assert prefs["delivery_updates"] is False and prefs["delivery_confirmations"] is False
    assert prefs["eta_updates"] is True and prefs["order_status_changes"] is True
    await device(world.customer)
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="10")
    async with SessionLocal() as s:
        row = await s.get(OrderRow, order.id)
        assert await step_notices.courier_step(s, row, "purchased", "on_the_way")
        row.status, row.delivered_at = "delivered", row.updated_at
        assert await step_notices.courier_step(s, row, "on_the_way", "delivered")
        await s.commit()
    assert len(await notifications(world.customer, "on_the_way")) == 1  # the in-app rows stay
    assert len(await notifications(world.customer, "delivered")) == 1
    assert await pushes(world.customer) == []  # both pushes turned off
    from app.services.notifications import push_skip_reason

    async with SessionLocal() as s:
        from app.models import User

        user = await s.get(User, world.customer.id)
        assert push_skip_reason(user, "on_the_way") == "delivery_updates_disabled"
        assert push_skip_reason(user, "eta_update") is None
        user.notify_eta_updates = False
        assert push_skip_reason(user, "delivery_delayed") == "eta_updates_disabled"


# --- m54: a new offer then a price change = ONE unread notice (the bell said +2 for one card) -------------


async def test_m54_one_unread_notice_per_offer(client, world):
    order = await world.order()
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 200, r.text
    offer_id = r.json()["offer"]["id"]
    edit = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 4})
    assert edit.status_code == 200, edit.text
    [note] = await notifications(world.customer, "new_offer")
    assert note.title_fr == "Offre modifiée" and note.read_at is None and note.data["edits"] == 1
    assert note.data["offer_id"] == offer_id and note.data["proposed_fee"] == 4.0
    unread = await client.get(
        "/api/entities/Notification", params={"q": '{"is_read": false}'}, headers=auth(world.customer)
    )
    assert len(unread.json()) == 1


# --- m9: a notification says whether its order is still open ---------------------------------------------


async def test_m9_notification_carries_the_order_status(client, world):
    from app.db import SessionLocal
    from app.services.notifications import notify

    order = await world.order(status="cancelled")
    async with SessionLocal() as s:
        await notify(
            s, user_id=world.courier_user.id, type_="new_order", title_ar="a", title_fr="f", order_id=order.id
        )
        await notify(s, user_id=world.courier_user.id, type_="account_verified", title_ar="a", title_fr="f")
        await s.commit()
    docs = (await client.get("/api/entities/Notification", headers=auth(world.courier_user))).json()
    by_type = {d["type"]: d for d in docs}
    assert by_type["new_order"]["order_status"] == "cancelled" and by_type["new_order"]["order_open"] is False
    assert (
        by_type["account_verified"]["order_status"] is None
        and by_type["account_verified"]["order_open"] is None
    )


# --- m30: changing the language moves the phone's pushes to it ---------------------------------------------


async def test_m30_language_change_follows_on_the_devices(client, world):
    from sqlalchemy import select

    from app.models import DeviceToken
    from tests.order_helpers import device, rows

    await device(world.customer, token="tok-lang")
    r = await client.patch("/api/auth/me", json={"language": "ar"}, headers=auth(world.customer))
    assert r.status_code == 200, r.text
    [token] = await rows(select(DeviceToken).where(DeviceToken.token == "tok-lang"))
    assert token.locale == "ar"
    r = await client.patch(
        f"/api/entities/UserProfile/{world.customer.id}",
        json={"language": "fr"},
        headers=auth(world.customer),
    )
    assert r.status_code == 200
    [token] = await rows(select(DeviceToken).where(DeviceToken.token == "tok-lang"))
    assert token.locale == "fr"
