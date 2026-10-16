"""/api/partner/v1 (Lot 2, 2A): keys, idempotence, orders, offers, cancel, tracking, availability,
the per-phone badge, D3 (10-minute wait), D4/D5 (returns), and the admin functions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import NoResponseCase, Order, Partner, PartnerWebhookDelivery, User
from app.services import prayer_times
from tests.factories import auth
from tests.launch_helpers import set_launch_mode
from tests.order_helpers import SOUSSE_HOME, TUNIS, OrderWorld, notifications, reload, rows, set_live
from tests.partner_helpers import PARTNER_API, incident, make_partner, order_body, partner_order, post_order

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
async def pw(client, world):
    return await make_partner(client, world.admin)


async def fn(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def count(model, *where) -> int:
    async with SessionLocal() as s:
        return int((await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


# --- admin functions and keys -----------------------------------------------------------------------


async def test_partner_functions_are_admin_only_and_never_leak_the_key(client, world, pw):
    for name in ("createPartner", "listPartners", "revokePartner", "rotatePartnerKey", "testPartnerWebhook"):
        r = await fn(client, world.customer, name, {"name": "x", "partner_id": str(pw.partner.id)})
        assert r.status_code == 403 and r.json()["error"] == "forbidden", name
    assert pw.api_key.startswith("odsd_live_") and len(pw.api_key) == len("odsd_live_") + 43
    listed = (await fn(client, world.admin, "listPartners")).json()
    [row] = listed["partners"]
    assert row["name"] == "Coffee and Tea" and row["key_prefix"] == pw.api_key[:14]
    assert row["has_webhook_secret"] is True and row["max_open_orders"] == 50 and row["open_orders"] == 0
    assert not {k for k in row if "key" in k and k != "key_prefix"} and "webhook_secret" not in row
    partner = await reload(Partner, pw.partner.id)
    assert partner.key_hash != pw.api_key and len(partner.key_hash) == 64
    tech = await reload(User, pw.technical_user_id)
    assert tech.role == "customer" and tech.password_hash is None and tech.full_name == "Coffee and Tea"


async def test_missing_wrong_revoked_and_rotated_keys_answer_401(client, world, pw):
    assert (await client.post(f"{PARTNER_API}/ping")).json()["error"] == "partner_key_required"
    wrong = await client.post(f"{PARTNER_API}/ping", headers={"X-Partner-Key": "odsd_live_nope"})
    assert wrong.status_code == 401 and wrong.json()["error"] == "partner_key_invalid"
    ok = await client.post(f"{PARTNER_API}/ping", headers=pw.headers)
    assert ok.status_code == 200 and ok.json()["partner"]["name"] == "Coffee and Tea"

    rotated = (await fn(client, world.admin, "rotatePartnerKey", {"partner_id": str(pw.partner.id)})).json()
    assert rotated["api_key"] != pw.api_key
    old = await client.post(f"{PARTNER_API}/ping", headers=pw.headers)
    assert old.status_code == 401 and old.json()["error"] == "partner_key_invalid"
    assert (
        await client.post(f"{PARTNER_API}/ping", headers={"X-Partner-Key": rotated["api_key"]})
    ).status_code == 200

    revoked = (await fn(client, world.admin, "revokePartner", {"partner_id": str(pw.partner.id)})).json()
    assert revoked["partner"]["active"] is False and revoked["partner"]["revoked_at"]
    gone = await client.post(f"{PARTNER_API}/ping", headers={"X-Partner-Key": rotated["api_key"]})
    assert gone.status_code == 401 and gone.json()["error"] == "partner_key_invalid"
    again = await fn(client, world.admin, "rotatePartnerKey", {"partner_id": str(pw.partner.id)})
    assert again.status_code == 409 and again.json()["error"] == "partner_revoked"
    assert (await fn(client, world.admin, "listPartners")).json()["partners"] == []
    assert (
        len((await fn(client, world.admin, "listPartners", {"include_revoked": True})).json()["partners"])
        == 1
    )


async def test_update_partner_validates_webhook_url_and_cap(client, world, pw):
    pid = str(pw.partner.id)
    bad = await fn(client, world.admin, "updatePartner", {"partner_id": pid, "webhook_url": "ftp://x"})
    assert bad.status_code == 400
    bad = await fn(client, world.admin, "updatePartner", {"partner_id": pid, "max_open_orders": 0})
    assert bad.status_code == 400
    ok = (
        await fn(
            client,
            world.admin,
            "updatePartner",
            {
                "partner_id": pid,
                "webhook_url": "http://partner.test/hook",
                "max_open_orders": 3,
                "name": "C&T",
            },
        )
    ).json()
    assert ok["partner"]["webhook_url"] == "http://partner.test/hook"
    assert ok["partner"]["max_open_orders"] == 3 and ok["partner"]["name"] == "C&T"
    missing = await fn(client, world.admin, "updatePartner", {"partner_id": "nope"})
    assert missing.status_code == 404 and missing.json()["error"] == "partner_not_found"


# --- POST /orders -----------------------------------------------------------------------------------


async def test_create_order_builds_the_order_as_place_order_and_dispatches(client, world, pw):
    r = await post_order(client, pw)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["courier_state"] == "searching" and body["dispatched"] == 1
    assert body["partner_ref"] == "ods-1-4321" and body["replayed"] is False and body["tracking_token"]
    assert body["customer_reliability"] == {
        "recent_orders": 0,
        "recent_unpaid": 0,
        "visible": False,
        "text_fr": None,
        "text_ar": None,
    }
    view = body["order"]
    assert view["items_total"] == 8.8 and view["courier"] is None and view["offers_count"] == 0
    assert view["test"] is False and view["created_at"].endswith("Z")

    order = await reload(Order, body["order_id"])
    assert (
        order.source == "partner" and order.partner_id == pw.partner.id and order.partner_ref == "ods-1-4321"
    )
    assert str(order.customer_id) == pw.technical_user_id
    assert order.contact_name == "Amel" and order.contact_phone_e164 == "+21622111222"
    assert order.items_text.splitlines() == [
        "[Restaurant ODS] Coffee and Tea",
        "2× Café crème — 3.500 DT",
        "1× Croissant — 1.800 DT",
        "Total repas : 8.800 DT",
    ]
    assert order.estimated_price == Decimal("8.800") and order.budget_max == Decimal("8.800")
    assert order.unavailable_policy == "call_me" and order.payment_method == "cash" and order.quantity == 3
    assert order.notes == "Sonner deux fois" and order.delivery_details == "3e étage"
    assert order.delivery_governorate == "Sousse" and order.preferred_courier_id is None

    [note] = await notifications(world.courier_user, "new_order")
    assert note.data["source"] == "partner" and note.data["partner_name"] == "Coffee and Tea"
    assert (
        note.data["courier_note_fr"] == "Appelez le client avant de payer."
        and note.data["items_total"] == 8.8
    )

    # the courier's Order document: label, note, price known in advance, clean badge
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert doc["source"] == "partner" and doc["partner_name"] == "Coffee and Tea"
    assert doc["partner_note_fr"] == "Appelez le client avant de payer."
    assert doc["partner_note_ar"] == "اتصل بالحريف قبل أن تدفع."
    assert doc["estimated_price"] == 8.8 and doc["budget_max"] == 8.8
    assert doc["partner_customer_recent_orders"] == 0 and doc["partner_customer_recent_unpaid"] == 0
    assert doc["partner_returns_refused"] == 0 and doc["partner_return_outcome"] is None
    assert doc["customer_name"] == "Amel" and doc["shop_name"] == "Coffee and Tea"
    # an app order carries none of it
    app_order = await world.order()
    doc = (await client.get(f"/api/entities/Order/{app_order.id}", headers=auth(world.courier_user))).json()
    assert doc["source"] == "app" and doc["partner_name"] is None and doc["partner_note_fr"] is None
    assert doc["partner_customer_recent_orders"] is None


async def test_same_idempotency_key_gives_the_same_order_and_one_broadcast(client, world, pw):
    body = order_body()
    body.pop("partner_ref")
    first = await post_order(client, pw, body, **{"Idempotency-Key": "ods-1-77"})
    second = await post_order(client, pw, body, **{"Idempotency-Key": "ods-1-77"})
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["order_id"] == first.json()["order_id"] and second.json()["replayed"] is True
    assert second.json()["partner_ref"] == "ods-1-77"
    assert await count(Order) == 1
    assert len(await notifications(world.courier_user, "new_order")) == 1

    other = order_body(items_total=12)
    other.pop("partner_ref")
    conflict = await post_order(client, pw, other, **{"Idempotency-Key": "ods-1-77"})
    assert conflict.status_code == 409 and conflict.json()["error"] == "idempotency_conflict"
    assert conflict.json()["order_id"] == first.json()["order_id"]

    mismatch = await post_order(client, pw, order_body(partner_ref="a"), **{"Idempotency-Key": "b"})
    assert mismatch.status_code == 400 and mismatch.json()["error"] == "partner_ref_mismatch"
    none = order_body()
    none.pop("partner_ref")
    assert (await post_order(client, pw, none)).status_code == 400

    # another partner may reuse the same ref: it is his own
    other_pw = await make_partner(client, world.admin, name="The Diesel")
    assert (await post_order(client, other_pw, order_body(partner_ref="ods-1-77"))).status_code == 201
    assert await count(Order) == 2


async def test_a_test_order_is_never_broadcast(client, world, pw):
    r = await post_order(client, pw, order_body(test=True))
    assert r.status_code == 201 and r.json()["dispatched"] == 0 and r.json()["order"]["test"] is True
    order = await reload(Order, r.json()["order_id"])
    assert order.items_text.startswith("QA TEST [Restaurant ODS] Coffee and Tea")
    assert await notifications(world.courier_user, "new_order") == []
    # a re-dispatch by hand stays silent too
    await fn(client, world.admin, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    assert await notifications(world.courier_user, "new_order") == []


@pytest.mark.parametrize(
    ("override", "status", "error"),
    [
        ({"customer": {"phone": "12345"}}, 400, "phone_required"),
        ({"customer": {"phone": "+33612345678"}}, 400, "phone_required"),
        ({"dropoff": {"lat": 48.85, "lng": 2.35}}, 400, "invalid_delivery_location"),
        ({"restaurant": {"lat": 48.85, "lng": 2.35}}, 400, "invalid_shop_location"),
        ({"items": []}, 400, "validation_error"),
        ({"items_total": 5000}, 400, "validation_error"),
        ({"currency": "EUR"}, 400, "validation_error"),
        ({"notes": "x" * 301}, 400, "validation_error"),
        ({"items": [{"name": "p" * 200, "qty": 1}] * 15}, 400, "items_too_long"),
    ],
)
async def test_create_order_validation(client, pw, override, status, error):
    r = await post_order(client, pw, order_body(**override))
    assert r.status_code == status and r.json()["error"] == error, r.text
    assert await count(Order) == 0


async def test_max_open_orders_of_the_partner_replaces_the_customer_cap(client, world, pw):
    await fn(client, world.admin, "updatePartner", {"partner_id": str(pw.partner.id), "max_open_orders": 1})
    assert (await post_order(client, pw, order_body(partner_ref="a"))).status_code == 201
    full = await post_order(client, pw, order_body(partner_ref="b"))
    assert full.status_code == 429 and full.json() == {
        "error": "too_many_open_orders",
        "message": "Too many open orders",
        "max": 1,
    }
    # the cap is the partner's, not the customer's 5: raise it, six orders go through
    await fn(client, world.admin, "updatePartner", {"partner_id": str(pw.partner.id), "max_open_orders": 10})
    for i in range(6):
        assert (await post_order(client, pw, order_body(partner_ref=f"c{i}"))).status_code == 201


# --- offers, accept, cancel -------------------------------------------------------------------------


async def test_offers_accept_and_cancel_through_the_partner_api(client, world, pw):
    order_id = (await post_order(client, pw)).json()["order_id"]
    other_pw = await make_partner(client, world.admin, name="The Diesel")
    assert (await client.get(f"{PARTNER_API}/orders/{order_id}", headers=other_pw.headers)).status_code == 404
    assert (await client.get(f"{PARTNER_API}/orders/nope", headers=pw.headers)).status_code == 404

    offer = await fn(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": order_id, "fee": 5, "eta_minutes": 15, "distance_km": 1.4, "message": "J'arrive"},
    )
    assert offer.status_code == 200, offer.text
    offer_id = offer.json()["offer"]["id"]

    listed = (await client.get(f"{PARTNER_API}/orders/{order_id}/offers", headers=pw.headers)).json()
    assert listed["status"] == "offers_received"
    [row] = listed["offers"]
    assert row["offer_id"] == offer_id and row["fee"] == 5 and row["eta_minutes"] == 15
    assert row["courier"] == {"first_name": "Karim", "rating": 5, "total_deliveries": 0, "vehicle": "scooter"}
    assert "phone" not in row["courier"] and row["created_at"].endswith("Z")
    view = (await client.get(f"{PARTNER_API}/orders/{order_id}", headers=pw.headers)).json()
    assert view["courier_state"] == "searching" and view["offers_count"] == 1 and view["courier"] is None

    stale = await client.post(
        f"{PARTNER_API}/orders/{order_id}/offers/{offer_id}/accept",
        json={"expected_fee": 4},
        headers=pw.headers,
    )
    assert stale.status_code == 409 and stale.json()["error"] == "offer_price_changed"
    accepted = await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "accepted" and body["delivery_fee"] == 5
    assert body["courier"]["first_name"] == "Karim" and body["courier"]["phone"] == "+21655123456"
    assert body["order"]["courier_state"] == "assigned" and body["order"]["accepted_at"]
    [note] = await notifications(world.courier_user, "order_accepted")
    assert note.order_id.hex == order_id.replace("-", "")
    again = await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    assert again.status_code == 409 and again.json()["error"] == "order_not_open"

    # free cancel while accepted (B25), then nothing once the courier is at the restaurant
    cancelled = await client.post(
        f"{PARTNER_API}/orders/{order_id}/cancel", json={"reason": "plat raté"}, headers=pw.headers
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    order = await reload(Order, order_id)
    assert order.cancelled_by == "customer" and order.cancel_reason == "plat raté"
    assert cancelled.json()["order"]["cancelled_by"] == "customer"

    second = (await post_order(client, pw, order_body(partner_ref="ods-1-2"))).json()["order_id"]
    offer_id = (
        await fn(client, world.courier_user, "createOrderOffer", {"order_id": second, "fee": 4})
    ).json()["offer"]["id"]
    await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    step = await client.patch(
        f"/api/entities/Order/{second}", json={"status": "at_shop"}, headers=auth(world.courier_user)
    )
    assert step.status_code == 200, step.text
    refused = await client.post(
        f"{PARTNER_API}/orders/{second}/cancel", json={"reason": "x"}, headers=pw.headers
    )
    assert refused.status_code == 409 and refused.json()["error"] == "cancel_not_allowed"
    assert refused.json()["contact"] == "courier" and refused.json()["status"] == "at_shop"
    view = (await client.get(f"{PARTNER_API}/orders/{second}", headers=pw.headers)).json()
    assert view["courier_state"] == "at_restaurant"


# --- public tracking --------------------------------------------------------------------------------


async def test_track_answers_without_account_and_without_the_courier_phone(client, world, pw):
    created = (await post_order(client, pw)).json()
    token = created["tracking_token"]
    assert (await client.get(f"{PARTNER_API}/track/unknown-token-xxxxxxxx")).status_code == 404
    assert (await client.get(f"{PARTNER_API}/track/{token}x")).status_code == 404
    waiting = await client.get(f"{PARTNER_API}/track/{token}")
    assert waiting.status_code == 200
    assert waiting.json() == {
        "status": "pending",
        "courier_state": "searching",
        "restaurant_name": "Coffee and Tea",
        "courier": None,
        "position": None,
        "eta_minutes": None,
        "delivery_fee": None,
        "delivered_at": None,
    }
    offer_id = (
        await fn(client, world.courier_user, "createOrderOffer", {"order_id": created["order_id"], "fee": 4})
    ).json()["offer"]["id"]
    await client.post(f"{PARTNER_API}/offers/{offer_id}/accept", json={}, headers=pw.headers)
    order = await reload(Order, created["order_id"])
    await set_live(order, world.courier, 35.8290, 10.6150)
    live = (await client.get(f"{PARTNER_API}/track/{token}")).json()
    assert live["status"] == "accepted" and live["courier_state"] == "assigned" and live["delivery_fee"] == 4
    assert live["courier"] == {"first_name": "Karim", "rating": 5, "vehicle": "scooter"}
    assert live["position"]["lat"] == pytest.approx(35.829) and live["position"]["updated_at"]
    assert live["eta_minutes"] is None or live["eta_minutes"] >= 1
    text = waiting.text + (await client.get(f"{PARTNER_API}/track/{token}")).text
    assert "+216" not in text and "Trabelsi" not in text and "plage" not in text and "8.8" not in text


async def test_track_is_rate_limited_per_ip(client, pw, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_PARTNER_TRACK", "2/hour")
    token = (await post_order(client, pw)).json()["tracking_token"]
    assert (await client.get(f"{PARTNER_API}/track/{token}")).status_code == 200
    assert (await client.get(f"{PARTNER_API}/track/{token}")).status_code == 200
    limited = await client.get(f"{PARTNER_API}/track/{token}")
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"


# --- availability -----------------------------------------------------------------------------------


async def test_availability_counts_couriers_without_notifying(client, world, pw, factory):
    far_user = await factory.user(email="far@example.test", profile=False)
    await world.make_courier(far_user, at=TUNIS)
    r = await client.get(
        f"{PARTNER_API}/availability",
        params={"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]},
        headers=pw.headers,
    )
    assert r.status_code == 200
    assert r.json() == {
        "serves": True,
        "couriers_free_within_10km": 1,
        "couriers_verified_within_10km": 1,
        "launch_mode": True,
    }
    assert await notifications(world.courier_user) == []
    paris = (
        await client.get(
            f"{PARTNER_API}/availability", params={"lat": 48.85, "lng": 2.35}, headers=pw.headers
        )
    ).json()
    assert paris["serves"] is False and paris["couriers_free_within_10km"] == 0
    # launch mode off: an offline courier is still « verified near », no longer « free »
    await set_launch_mode(False)
    async with SessionLocal() as s:
        courier = await s.get(type(world.courier), world.courier.id)
        courier.is_online = False
        await s.commit()
    r = (
        await client.get(
            f"{PARTNER_API}/availability",
            params={"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]},
            headers=pw.headers,
        )
    ).json()
    assert r == {
        "serves": True,
        "couriers_free_within_10km": 0,
        "couriers_verified_within_10km": 1,
        "launch_mode": False,
    }
    assert (
        await client.get(f"{PARTNER_API}/availability", params={"lat": 1}, headers=pw.headers)
    ).status_code == 400


# --- the badge per phone number ---------------------------------------------------------------------


async def test_customer_badge_is_counted_per_phone_over_partner_orders(client, world, pw):
    paid = await partner_order(world, pw, status="delivered", courier=world.courier, fee="4", purchase="20")
    unpaid = await partner_order(world, pw, status="cancelled", courier=world.courier, fee="4", purchase="20")
    await incident(unpaid)
    # the same number on an APP order of another customer never counts here
    await world.order(status="delivered", courier=world.courier)
    other_phone = await partner_order(
        world, pw, status="delivered", courier=world.courier, phone="+21699999999"
    )
    await incident(other_phone)

    created = (await post_order(client, pw)).json()
    assert created["customer_reliability"] == {
        "recent_orders": 2,
        "recent_unpaid": 1,
        "visible": True,
        "text_fr": "Ce client n'a pas payé 1 fois sur ses 2 dernières commandes",
        "text_ar": "هذا الحريف لم يدفع مرة واحدة من آخر طلبين",
    }
    doc = (
        await client.get(f"/api/entities/Order/{created['order_id']}", headers=auth(world.courier_user))
    ).json()
    assert doc["partner_customer_recent_orders"] == 2 and doc["partner_customer_recent_unpaid"] == 1
    doc = (await client.get(f"/api/entities/Order/{paid.id}", headers=auth(world.admin))).json()
    assert doc["partner_customer_recent_orders"] == 2 and doc["partner_customer_recent_unpaid"] == 1

    # getCustomerReliability for the courier: the number's record, not the technical user's
    r = (
        await fn(client, world.courier_user, "getCustomerReliability", {"order_id": created["order_id"]})
    ).json()
    assert r["recent_orders"] == 2 and r["recent_unpaid"] == 1 and r["visible_to_couriers"] is True
    assert r["incidents"] == 1 and r["delivered_orders"] == 1 and r["suspended"] is False
    assert r["avg_reply_seconds"] is None
    clean = (await post_order(client, pw, order_body(partner_ref="z", customer={"phone": "20000000"}))).json()
    assert clean["customer_reliability"]["visible"] is False
    r = (
        await fn(client, world.courier_user, "getCustomerReliability", {"order_id": clean["order_id"]})
    ).json()
    assert r["recent_orders"] == 0 and r["incidents"] == 0 and r["visible_to_couriers"] is False


def test_badge_wording_matches_the_courier_app():
    from app.services.partner_stats import badge_texts

    assert badge_texts(0, 0) == (None, None) and badge_texts(5, 0) == (None, None)
    assert badge_texts(1, 1) == (
        "Ce client n'a pas payé sa dernière commande",
        "هذا الحريف لم يدفع ثمن طلبه الأخير",
    )
    assert badge_texts(10, 2) == (
        "Ce client n'a pas payé 2 fois sur ses 10 dernières commandes",
        "هذا الحريف لم يدفع مرتين من آخر 10 طلبات",
    )
    assert badge_texts(3, 3)[1] == "هذا الحريف لم يدفع 3 مرات من آخر 3 طلبات"


# --- D3: 10 minutes around the prayers, at the iftar, on Ramadan nights -----------------------------


def test_prayer_slots_for_sousse(monkeypatch):
    times = prayer_times.prayer_times(datetime(2026, 10, 16, tzinfo=UTC).date())
    assert [t.strftime("%H:%M") for t in times.values()] == [
        "04:00",
        "11:03",
        "14:13",
        "16:41",
        "18:06",
    ]  # UTC
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 16, 50, tzinfo=UTC)) == "maghrib"
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 16, 37, tzinfo=UTC)) == "maghrib"  # 5 min before
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 17, 10, tzinfo=UTC)) is None  # 29 min after
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 13, 0, tzinfo=UTC)) is None
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 23, 0, tzinfo=UTC)) is None
    monkeypatch.setattr(prayer_times, "_is_ramadan", lambda day: True)
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 16, 30, tzinfo=UTC)) == "iftar"
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 23, 0, tzinfo=UTC)) == "ramadan_night"
    assert prayer_times.slow_slot(datetime(2026, 10, 17, 3, 0, tzinfo=UTC)) == "ramadan_night"
    assert prayer_times.slow_slot(datetime(2026, 10, 16, 13, 0, tzinfo=UTC)) is None
    assert prayer_times.wait_seconds("partner", datetime(2026, 10, 16, 23, 0, tzinfo=UTC)) == 600
    assert prayer_times.wait_seconds("app", datetime(2026, 10, 16, 23, 0, tzinfo=UTC)) == 180
    assert prayer_times.wait_seconds("partner", datetime(2026, 10, 16, 13, 0, tzinfo=UTC)) == 180


async def test_no_response_waits_ten_minutes_for_partner_orders_in_a_slow_slot(
    client, world, pw, monkeypatch
):
    monkeypatch.setattr(prayer_times, "slow_slot", lambda now, where=None: "isha")
    partner = await partner_order(
        world, pw, status="on_the_way", courier=world.courier, fee="4", purchase="20", at_door=True
    )
    r = await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"order_id": str(partner.id), "action": "report_no_response"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["timeout_seconds"] == 600 and 598 <= r.json()["seconds_left"] <= 600
    [case] = await rows(select(NoResponseCase).where(NoResponseCase.order_id == partner.id))
    assert case.deadline_at - case.started_at == timedelta(minutes=10)
    [alert] = await notifications(await reload(User, pw.technical_user_id), "emergency_contact")
    assert "sous 10 min" in alert.body_fr
    view = (await client.get(f"{PARTNER_API}/orders/{partner.id}", headers=pw.headers)).json()
    assert view["courier_state"] == "incident_no_response" and view["no_response"]["wait_seconds"] == 600

    app_order = await world.order(
        status="on_the_way", courier=world.courier, fee="4", purchase="20", at_door=True
    )
    # the courier is on the partner order: an admin reports for the app order (same rule otherwise)
    r = await fn(
        client,
        world.admin,
        "triggerEmergencyContact",
        {"order_id": str(app_order.id), "action": "report_no_response"},
    )
    assert r.status_code == 200 and r.json()["timeout_seconds"] == 180


async def test_no_response_keeps_three_minutes_outside_the_slots(client, world, pw, monkeypatch):
    monkeypatch.setattr(prayer_times, "slow_slot", lambda now, where=None: None)
    partner = await partner_order(
        world, pw, status="on_the_way", courier=world.courier, fee="4", purchase="20", at_door=True
    )
    r = await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"order_id": str(partner.id), "action": "report_no_response"},
    )
    assert r.json()["timeout_seconds"] == 180


# --- D4 / D5: the meal brought back ----------------------------------------------------------------


async def test_report_partner_return_refused_and_shop_closed(client, world, pw):
    await fn(
        client,
        world.admin,
        "updatePartner",
        {"partner_id": str(pw.partner.id), "webhook_url": "http://partner.test/h"},
    )
    first = await partner_order(
        world, pw, status="client_no_response", courier=world.courier, fee="4", purchase="20"
    )
    app_order = await world.order(status="client_no_response", courier=world.courier, fee="4", purchase="20")
    stranger = await fn(
        client, world.customer, "reportPartnerReturn", {"order_id": str(first.id), "outcome": "refused"}
    )
    assert stranger.status_code == 403
    bad = await fn(
        client, world.courier_user, "reportPartnerReturn", {"order_id": str(first.id), "outcome": "kept"}
    )
    assert bad.status_code == 400
    not_partner = await fn(
        client,
        world.courier_user,
        "reportPartnerReturn",
        {"order_id": str(app_order.id), "outcome": "refused"},
    )
    assert not_partner.status_code == 409 and not_partner.json()["error"] == "not_partner_order"

    refused = await fn(
        client, world.courier_user, "reportPartnerReturn", {"order_id": str(first.id), "outcome": "refused"}
    )
    assert refused.status_code == 200 and refused.json() == {
        "success": True,
        "outcome": "refused",
        "returns_refused": 1,
        "debt_amount": 0.0,
    }
    again = await fn(
        client,
        world.courier_user,
        "reportPartnerReturn",
        {"order_id": str(first.id), "outcome": "shop_closed"},
    )
    assert again.status_code == 409 and again.json()["error"] == "already_reported"
    doc = (await client.get(f"/api/entities/Order/{first.id}", headers=auth(world.courier_user))).json()
    assert doc["partner_returns_refused"] == 1 and doc["partner_return_outcome"] == "refused"
    [event] = await rows(
        select(PartnerWebhookDelivery).where(PartnerWebhookDelivery.event == "order.return_refused")
    )
    assert event.payload["data"]["returns_refused"] == 1 and event.status == "pending"

    second = await partner_order(
        world, pw, status="client_no_response", courier=world.courier, fee="4", purchase="25"
    )
    closed = await fn(
        client, world.admin, "reportPartnerReturn", {"order_id": str(second.id), "outcome": "shop_closed"}
    )
    assert closed.status_code == 200 and closed.json()["debt_amount"] == 29.0
    partner = await reload(Partner, pw.partner.id)
    assert partner.debt_amount == Decimal("29.000") and partner.debt_since is not None
    blocked = await post_order(client, pw)
    assert blocked.status_code == 409 and blocked.json()["error"] == "restaurant_debt_unpaid"
    assert blocked.json()["amount"] == 29.0
    [debt] = await rows(
        select(PartnerWebhookDelivery).where(PartnerWebhookDelivery.event == "order.restaurant_debt")
    )
    assert debt.payload["data"] == {"amount": 29.0, "debt_amount": 29.0}
    doc = (await client.get(f"/api/entities/Order/{second.id}", headers=auth(world.courier_user))).json()
    assert doc["partner_debt_amount"] == 29.0

    settled = (await fn(client, world.admin, "settlePartnerDebt", {"partner_id": str(pw.partner.id)})).json()
    assert settled["partner"]["debt_amount"] == 0 and settled["partner"]["debt_since"] is None
    assert (await post_order(client, pw)).status_code == 201
    assert await count(PartnerWebhookDelivery, PartnerWebhookDelivery.event == "partner.debt_settled") == 1


async def test_the_courier_returning_the_meal_marks_the_order_accepted_back(client, world, pw):
    order = await partner_order(
        world, pw, status="client_no_response", courier=world.courier, fee="4", purchase="20"
    )
    await incident(order, status="expired", resolution=None)  # the procedure ran out, nobody answered
    r = await fn(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": "goods_returned_to_shop",
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )
    assert r.status_code == 200, r.text
    order = await reload(Order, order.id)
    assert order.status == "cancelled" and order.partner_return_outcome == "accepted"
    view = (await client.get(f"{PARTNER_API}/orders/{order.id}", headers=pw.headers)).json()
    assert view["return_outcome"] == "accepted" and view["cancel_reason"] == "goods_returned_to_shop"
