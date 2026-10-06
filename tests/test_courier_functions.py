"""updateMyCourierProfile, trackCourierLocation, getOrderCourier, getOrderETA, rateCourier,
reportOrderIssue, getCustomerReliability, getCourierIdPhotos."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.integrations import osrm
from app.models import Courier, File, NoResponseCase, OrderIssue, OrderRating, OrderTracking
from tests.factories import auth
from tests.order_helpers import (
    SOUSSE_HOME,
    SOUSSE_SHOP,
    OrderWorld,
    age_order,
    notifications,
    pushes,
    reload,
    rows,
    set_live,
)


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


# --- updateMyCourierProfile -------------------------------------------------------------------------


async def private_upload(user, key=None) -> str:
    key = key or f"private/generic/{user.id}/id.jpg"
    async with SessionLocal() as s:
        s.add(File(key=key, owner_id=user.id, visibility="private", content_type="image/jpeg", size_bytes=3))
        await s.commit()
    return key


ONBOARDING = {
    "full_name": "Sami Ben Salah",
    "phone": "22 333 444",
    "cin_passport": "08123456",
    "vehicle_type": "car",
    "price_per_km": 2,
    "min_fee": 3,
    "service_country": "tn",
    "service_governorate": "Sousse",
}


async def test_onboarding_creates_a_pending_offline_profile(client, factory):
    user = await factory.user(email="new@example.test")
    key = await private_upload(user)
    fields = {
        **ONBOARDING,
        "id_photo_uri": key,
        "verification_status": "verified",
        "total_deliveries": 50,
        "is_online": True,
    }
    r = await call(client, user, "updateMyCourierProfile", {"action": "create", "fields": fields})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["ignored"]) == ["total_deliveries", "verification_status"]
    profile = body["profile"]
    assert profile["verification_status"] == "pending" and profile["is_online"] is False
    assert profile["phone"] == "+21622333444" and profile["service_country"] == "TN"
    assert profile["total_deliveries"] == 0 and "id_photo_uri" not in profile
    row = (await rows(select(Courier)))[0]
    assert row.id_document_key == key
    photo = (await rows(select(File).where(File.key == key)))[0]
    assert photo.purpose == "courier_id"  # only the admin function signs it from now on
    signed = await client.post("/api/files/signed-url", json={"file_uri": key}, headers=auth(user))
    assert signed.status_code == 403

    again = await call(
        client, user, "updateMyCourierProfile", {"action": "create", "fields": {"full_name": "X"}}
    )
    assert again.json()["existed"] is True and again.json()["profile"]["full_name"] == "Sami Ben Salah"


async def test_onboarding_refusals(client, factory):
    user = await factory.user(email="new@example.test")
    other = await factory.user(email="other@example.test")
    theirs = await private_upload(other)
    r = await call(
        client,
        user,
        "updateMyCourierProfile",
        {"action": "create", "fields": {**ONBOARDING, "id_photo_uri": theirs}},
    )
    assert r.status_code == 400 and r.json() == {"error": "invalid_fields", "fields": ["id_photo_uri"]}
    public = await call(
        client,
        user,
        "updateMyCourierProfile",
        {"action": "create", "fields": {**ONBOARDING, "id_photo_uri": "https://x/y.jpg"}},
    )
    assert public.json()["fields"] == ["id_photo_uri"]
    bad = await call(
        client,
        user,
        "updateMyCourierProfile",
        {
            "action": "create",
            "fields": {**ONBOARDING, "price_per_km": 80, "phone": "12", "vehicle_type": "jet"},
        },
    )
    assert bad.status_code == 400 and sorted(bad.json()["fields"]) == [
        "phone",
        "price_per_km",
        "vehicle_type",
    ]
    missing = await call(
        client,
        user,
        "updateMyCourierProfile",
        {"action": "create", "fields": {"full_name": "A", "vehicle_type": "car"}},
    )
    assert missing.json() == {"error": "missing_fields", "fields": ["phone", "price_per_km"]}
    assert await rows(select(Courier)) == []
    none = await call(
        client, user, "updateMyCourierProfile", {"action": "update", "fields": {"is_online": True}}
    )
    assert none.status_code == 404 and none.json() == {"error": "no_courier_profile"}


async def test_profile_update_and_record_delivery(client, world, factory):
    r = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {
            "fields": {
                "is_online": False,
                "current_lat": 35.9,
                "current_lng": 10.5,
                "notification_radius_km": "15",
                "service_start_time": "08:00",
                "service_end_time": "",
                "verification_status": "verified",
                "full_name": "X",
            }
        },
    )
    assert r.status_code == 200, r.text
    profile = r.json()["profile"]
    assert profile["is_online"] is False and profile["current_lat"] == pytest.approx(35.9)
    assert profile["notification_radius_km"] == 15 and profile["service_start_time"] == "08:00"
    assert profile["service_end_time"] is None and profile["full_name"] == "Karim Trabelsi"
    assert sorted(r.json()["ignored"]) == ["full_name", "verification_status"]
    bad = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"fields": {"service_start_time": "25:00", "current_lat": 100}},
    )
    assert bad.status_code == 400 and sorted(bad.json()["fields"]) == ["current_lat", "service_start_time"]
    empty = await call(client, world.courier_user, "updateMyCourierProfile", {"fields": {}})
    assert empty.json()["success"] is True

    delivered = await world.order(status="delivered", courier=world.courier, fee="4")
    running = await world.order(status="on_the_way", courier=world.courier, fee="4")
    ok = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"action": "record_delivery", "order_id": str(delivered.id)},
    )
    assert ok.status_code == 200 and ok.json()["profile"]["total_deliveries"] == 1
    assert ok.json()["profile"]["total_earnings"] == 4
    early = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"action": "record_delivery", "order_id": str(running.id)},
    )
    assert early.status_code == 409
    assert (
        await call(client, world.courier_user, "updateMyCourierProfile", {"action": "record_delivery"})
    ).status_code == 400
    assert (
        await call(
            client,
            world.courier_user,
            "updateMyCourierProfile",
            {"action": "record_delivery", "order_id": "x"},
        )
    ).status_code == 404
    someone_user = await factory.user(email="c3@example.test", profile=False)
    other = await world.order(status="delivered", courier=await world.make_courier(someone_user), fee="5")
    assert (
        await call(
            client,
            world.courier_user,
            "updateMyCourierProfile",
            {"action": "record_delivery", "order_id": str(other.id)},
        )
    ).status_code == 403
    assert (await call(client, world.courier_user, "updateMyCourierProfile", {"action": "boom"})).json() == {
        "error": "unknown_action"
    }


async def test_a_position_abroad_is_never_published(client, world):
    """A courier serving Tunisia: a fix outside it (phone abroad) is dropped, the rest is saved."""
    r = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"fields": {"is_online": True, "current_lat": 48.8566, "current_lng": 2.3522}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["ignored"]) == ["current_lat", "current_lng"]
    assert body["profile"]["is_online"] is True
    # The previous (Sousse) position is kept.
    assert body["profile"]["current_lat"] == pytest.approx(SOUSSE_SHOP[0])
    assert body["profile"]["current_lng"] == pytest.approx(SOUSSE_SHOP[1])
    # A fix inside Tunisia, with a real phone's full precision, still goes through.
    ok = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"fields": {"current_lat": 35.82561234567891, "current_lng": 10.60841234567891}},
    )
    assert ok.json()["ignored"] == []
    assert ok.json()["profile"]["current_lat"] == pytest.approx(35.8256123, abs=1e-6)
    # Only one coordinate, outside: dropped too (never half a position).
    half = await call(
        client, world.courier_user, "updateMyCourierProfile", {"fields": {"current_lat": 48.85}}
    )
    assert half.json()["ignored"] == ["current_lat"]
    # A courier serving another country keeps his own positions.
    moved = await call(
        client,
        world.courier_user,
        "updateMyCourierProfile",
        {"fields": {"service_country": "FR", "current_lat": 48.8566, "current_lng": 2.3522}},
    )
    assert moved.json()["ignored"] == []
    assert moved.json()["profile"]["current_lat"] == pytest.approx(48.8566)


async def test_onboarding_abroad_keeps_no_position(client, factory):
    user = await factory.user(email="abroad@example.test", profile=False)
    await private_upload(user)
    fields = {**ONBOARDING, "current_lat": 48.8566, "current_lng": 2.3522}
    r = await call(client, user, "updateMyCourierProfile", {"action": "create", "fields": fields})
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["current_lat"] is None
    assert sorted(r.json()["ignored"]) == ["current_lat", "current_lng"]


async def test_courier_address_is_the_accounts_default_address(client, world, factory):
    address = {
        "address": " 12 rue de la Plage ",
        "address_city": "Hammam Sousse",
        "address_governorate": "Sousse",
        "address_lat": 35.8611,
        "address_lng": 10.5947,
    }
    r = await call(client, world.courier_user, "updateMyCourierProfile", {"fields": address})
    assert r.status_code == 200, r.text
    profile = r.json()["profile"]
    assert profile["address"] == "12 rue de la Plage"
    assert profile["address_city"] == "Hammam Sousse" and profile["address_governorate"] == "Sousse"
    assert profile["address_lat"] == pytest.approx(35.8611) and profile["address_lng"] == pytest.approx(
        10.5947
    )
    # A courier-only account does not become a customer by saving his address.
    mine = await client.get("/api/entities/UserProfile", headers=auth(world.courier_user))
    assert mine.status_code == 200 and mine.json() == []
    read = await client.get("/api/entities/CourierProfile", headers=auth(world.courier_user))
    assert read.json()[0]["address"] == "12 rue de la Plage"
    # Coordinates go together.
    bad = await call(client, world.courier_user, "updateMyCourierProfile", {"fields": {"address_lat": 36.0}})
    assert bad.status_code == 400 and bad.json()["fields"] == ["address_lng"]

    # A customer+courier account: one address, the customer profile's.
    dual = await factory.user(email="dual@example.test")
    await world.make_courier(dual, display_name="Dual")
    created = await client.put(
        f"/api/entities/UserProfile/{dual.id}",
        json={
            "default_address": "5 avenue Habib Bourguiba",
            "city": "Sousse",
            "governorate": "Sousse",
            "default_lat": 35.8256,
            "default_lng": 10.6084,
        },
        headers=auth(dual),
    )
    assert created.status_code == 200, created.text
    seen = (await client.get("/api/entities/CourierProfile", headers=auth(dual))).json()[0]
    assert seen["address"] == "5 avenue Habib Bourguiba" and seen["address_lat"] == pytest.approx(35.8256)
    await call(client, dual, "updateMyCourierProfile", {"fields": {"address": "7 rue de Tunis"}})
    customer = (await client.get(f"/api/entities/UserProfile/{dual.id}", headers=auth(dual))).json()
    assert customer["default_address"] == "7 rue de Tunis" and customer["city"] == "Sousse"


# --- trackCourierLocation (base44/tests/live_position_test.ts) ----------------------------------------


async def track(client, world, lat=35.83, lng=10.61, user=None, courier_id=None):
    return await call(
        client,
        user or world.courier_user,
        "trackCourierLocation",
        {"courier_id": courier_id or str(world.courier.id), "lat": lat, "lng": lng},
    )


async def test_fix_goes_onto_active_orders_only(client, world, factory):
    active = await world.order(status="on_the_way", courier=world.courier)
    shopping = await world.order(status="at_shop", courier=world.courier)
    await world.order(status="delivered", courier=world.courier, fee="5")
    await world.order()  # open
    stale = await world.order(status="accepted", courier=world.courier)
    await age_order(stale, 49)
    r = await track(client, world)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_orders"] == 2 and body["validation_status"] == "all_checks_passed"
    assert body["updated_at"].endswith("Z")
    live = {t.order_id for t in await rows(select(OrderTracking))}
    assert live == {active.id, shopping.id}  # never onto the abandoned one
    courier = await reload(Courier, world.courier.id)
    assert courier.last_seen_at is not None
    doc = (await client.get(f"/api/entities/Order/{active.id}", headers=auth(world.customer))).json()
    assert doc["courier_live_lat"] == pytest.approx(35.83)
    # a second fix moves it (upsert)
    await track(client, world, lat=35.84)
    doc = (await client.get(f"/api/entities/Order/{active.id}", headers=auth(world.customer))).json()
    assert doc["courier_live_lat"] == pytest.approx(35.84)


async def test_another_couriers_id_is_refused(client, world, factory):
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user)
    await world.order(status="on_the_way", courier=other)
    r = await track(client, world, courier_id=str(other.id))
    assert r.status_code == 403 and r.json() == {"error": "Unauthorized"}
    assert await rows(select(OrderTracking)) == []
    nobody = await factory.user(email="n@example.test")
    assert (await track(client, world, user=nobody)).status_code == 403


@pytest.mark.parametrize(
    ("lat", "lng", "reason"),
    [
        ("35.8", 10.6, "Coordinates must be numbers"),
        (48.85, 2.35, "Latitude 48.85 outside valid range [30.0, 37.5]"),
        (35.8, 20.0, "Longitude 20.0 outside valid range [8.0, 12.5]"),
    ],
)
async def test_invalid_coordinates(client, world, lat, lng, reason):
    r = await track(client, world, lat=lat, lng=lng)
    assert r.status_code == 400
    assert r.json() == {
        "error": "Invalid coordinates",
        "reason": reason,
        "validation_status": "geo_validation_failed",
    }


async def test_missing_fields(client, world):
    r = await call(client, world.courier_user, "trackCourierLocation", {"lat": 35.8})
    assert r.status_code == 400 and r.json()["error"] == "Missing required fields: courier_id, lat, lng"


async def test_live_position_leaves_with_the_delivery(client, world):
    """A later transition out of the delivery takes the position off (CLEAR_LIVE_POSITION)."""
    order = await world.order(status="on_the_way", courier=world.courier, fee="5")
    await track(client, world)
    r = await client.patch(
        f"/api/entities/Order/{order.id}", json={"status": "delivered"}, headers=auth(world.courier_user)
    )
    assert r.status_code == 200 and r.json()["courier_live_lat"] is None
    assert await rows(select(OrderTracking)) == []


# --- getOrderCourier ------------------------------------------------------------------------------


async def test_order_courier_card(client, world, factory):
    open_order = await world.order()
    r = await call(client, world.customer, "getOrderCourier", {"order_id": str(open_order.id)})
    assert r.json() == {"success": True, "courier": None, "tracking": False}
    order = await world.order(status="on_the_way", courier=world.courier)
    await set_live(order, world.courier, 35.9, 10.7, datetime.now(UTC) + timedelta(seconds=5))
    r = await call(client, world.customer, "getOrderCourier", {"order_id": str(order.id)})
    body = r.json()
    assert body["tracking"] is True and body["verified"] is True
    card = body["courier"]
    assert card["full_name"] == "Karim Trabelsi" and card["first_name"] == "Karim"
    assert card["phone"] == "+21655123456" and card["average_rating"] == 5 and card["total_deliveries"] == 0
    assert card["current_lat"] == pytest.approx(35.9)  # the fresher order fix wins
    assert "cin_passport" not in card and "id_photo_uri" not in card and "user_id" not in card
    delivered = await world.order(status="delivered", courier=world.courier, fee="5")
    body = (await call(client, world.customer, "getOrderCourier", {"order_id": str(delivered.id)})).json()
    assert body["tracking"] is False and body["courier"]["current_lat"] is None
    stranger = await factory.user(email="s@example.test")
    assert (await call(client, stranger, "getOrderCourier", {"order_id": str(order.id)})).status_code == 403
    assert (
        await call(client, stranger, "getOrderCourier", {"order_id": str(open_order.id)})
    ).status_code == 403
    assert (
        await call(client, world.courier_user, "getOrderCourier", {"order_id": str(order.id)})
    ).status_code == 200
    assert (await call(client, world.customer, "getOrderCourier", {})).status_code == 400
    assert (await call(client, world.customer, "getOrderCourier", {"order_id": "zz"})).status_code == 404


async def test_offer_courier_card(client, world, factory):
    order = await world.order(status="offers_received")
    offer = await world.offer(order)
    r = await call(client, world.customer, "getOrderCourier", {"offer_id": str(offer.id)})
    assert r.json()["courier"]["phone"] == "+21655123456" and r.json()["courier"]["current_lat"] is None
    stranger = await factory.user(email="s@example.test")
    assert (await call(client, stranger, "getOrderCourier", {"offer_id": str(offer.id)})).status_code == 403
    assert (
        await call(client, world.admin, "getOrderCourier", {"offer_id": str(offer.id)})
    ).status_code == 200
    assert (await call(client, world.customer, "getOrderCourier", {"offer_id": "nope"})).status_code == 404


# --- getOrderETA --------------------------------------------------------------------------------------


@pytest.fixture
def osrm_answer(monkeypatch):
    calls = []

    def answer(response):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return response(request) if callable(response) else response

        monkeypatch.setattr(settings, "OSRM_URL", "http://osrm.test")
        monkeypatch.setattr(osrm, "transport", httpx.MockTransport(handler))
        return calls

    return answer


async def eta(client, user, order):
    return (await call(client, user, "getOrderETA", {"order_id": str(order.id)})).json()


async def test_eta_through_osrm(client, world, osrm_answer):
    geometry = {"type": "LineString", "coordinates": [[10.6084, 35.8256], [10.61, 35.83], "junk"]}
    calls = osrm_answer(
        httpx.Response(200, json={"routes": [{"duration": 610, "distance": 4321, "geometry": geometry}]})
    )
    order = await world.order(status="accepted", courier=world.courier)
    body = await eta(client, world.customer, order)
    assert body == {
        "success": True,
        "order_id": str(order.id),
        "status": "accepted",
        "eta_minutes": 11,
        "distance_km": 4.32,
        "source": "osrm",
        "route_coords": [[35.8256, 10.6084], [35.83, 10.61]],
    }
    assert calls[0].startswith(f"http://osrm.test/route/v1/driving/{SOUSSE_SHOP[1]},{SOUSSE_SHOP[0]};")
    assert "geometries=geojson" in calls[0] and "overview=full" in calls[0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"routes": []}),
        httpx.Response(502, text="bad gateway"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
    ],
)
async def test_eta_fallback(client, world, osrm_answer, response):
    osrm_answer(response)
    order = await world.order(status="on_the_way", courier=world.courier)
    body = await eta(client, world.customer, order)
    assert body["source"] == "haversine_fallback" and body["eta_minutes"] >= 1
    assert body["distance_km"] == pytest.approx(1.18, abs=0.05)  # shop → home


async def test_eta_reasons(client, world, factory):
    assert (await eta(client, world.customer, await world.order()))["reason"] == "inactive_status"
    no_position_user = await factory.user(email="np@example.test", profile=False)
    no_position = await world.make_courier(no_position_user, at=None)
    order = await world.order(status="accepted", courier=no_position)
    assert (await eta(client, world.customer, order))["reason"] == "courier_location_unavailable"
    no_dest = await world.order(status="on_the_way", courier=world.courier, delivery=None)
    assert (await eta(client, world.customer, no_dest))["reason"] == "destination_unavailable"
    walking_user = await factory.user(email="w@example.test", profile=False)
    walker = await world.make_courier(walking_user, vehicle="walking", at=SOUSSE_HOME)
    far = await world.order(status="accepted", courier=walker)
    assert (await eta(client, world.customer, far))["eta_minutes"] == 14  # 1.16 km at 5 km/h
    stranger = await factory.user(email="s@example.test")
    assert (await call(client, stranger, "getOrderETA", {"order_id": str(order.id)})).status_code == 403
    assert (await call(client, stranger, "getOrderETA", {})).status_code == 400
    assert (await call(client, stranger, "getOrderETA", {"order_id": "nope"})).status_code == 404


# --- rateCourier -------------------------------------------------------------------------------------


async def test_rate_courier(client, world, factory):
    first = await world.order(status="delivered", courier=world.courier, fee="5")
    second = await world.order(status="delivered", courier=world.courier, fee="5")
    await set_live(first, world.courier, 35.8, 10.6)
    r = await call(
        client, world.customer, "rateCourier", {"order_id": str(first.id), "rating": 4.4, "comment": "bien"}
    )
    assert r.json() == {"success": True, "average_rating": 4.0}
    r = await call(client, world.customer, "rateCourier", {"order_id": str(second.id), "rating": 5})
    assert r.json()["average_rating"] == 4.5
    r = await call(client, world.customer, "rateCourier", {"order_id": str(first.id), "rating": 2})
    assert r.json()["average_rating"] == 3.5  # a new rating replaces the order's
    assert len(await rows(select(OrderRating))) == 2 and await rows(select(OrderTracking)) == []
    doc = (await client.get(f"/api/entities/Order/{first.id}", headers=auth(world.customer))).json()
    assert doc["customer_rating"] == 2 and doc["rating_comment"] == ""
    for payload in ({"order_id": str(first.id)}, {"order_id": str(first.id), "rating": 6}, {"rating": 3}):
        assert (await call(client, world.customer, "rateCourier", payload)).status_code == 400
    assert (
        await call(client, world.customer, "rateCourier", {"order_id": "x", "rating": 3})
    ).status_code == 404
    running = await world.order(status="on_the_way", courier=world.courier)
    r = await call(client, world.customer, "rateCourier", {"order_id": str(running.id), "rating": 3})
    assert r.status_code == 409 and r.json() == {"error": "order_not_delivered"}
    r = await call(client, world.courier_user, "rateCourier", {"order_id": str(first.id), "rating": 5})
    assert r.status_code == 403


# --- reportOrderIssue -------------------------------------------------------------------------------


async def test_report_issue(client, world):
    order = await world.order(status="at_shop", courier=world.courier)
    photo = f"{settings.public_files_base_url}/public/issue/2026/09/p.jpg"
    payload = {
        "order_id": str(order.id),
        "courier_id": str(world.courier.id),
        "issue_type": "product_damaged",
        "description": " cassé ",
        "photo_url": photo,
    }
    r = await call(client, world.courier_user, "reportOrderIssue", payload)
    assert r.json() == {"success": True, "message": "Issue reported successfully"}
    [note] = await notifications(world.customer, "issue_reported")
    assert note.body_fr == "Le livreur a signalé un problème: Produit endommagé"
    assert note.data == {"issue_type": "product_damaged", "description": "cassé"}
    [admin_note] = await notifications(world.admin, "issue_reported")
    assert admin_note.body_fr == f"Commande #{str(order.id)[-6:].upper()}: Produit endommagé"
    assert admin_note.data["photo_url"] == photo
    assert await pushes(world.customer) == []  # in-app only, like the live function
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["has_issues"] is True and doc["reported_issues"][0]["photo_url"] == photo
    for _ in range(4):
        await call(client, world.courier_user, "reportOrderIssue", {**payload, "photo_url": "https://evil/x"})
    assert (await rows(select(OrderIssue)))[-1].photo_key is None
    full = await call(client, world.courier_user, "reportOrderIssue", payload)
    assert full.status_code == 429 and full.json() == {"error": "too_many_reports"}


async def test_report_issue_guards(client, world, factory):
    order = await world.order(status="at_shop", courier=world.courier)
    base = {"order_id": str(order.id), "courier_id": str(world.courier.id), "issue_type": "other"}
    assert (
        await call(client, world.courier_user, "reportOrderIssue", {"order_id": str(order.id)})
    ).status_code == 400
    bad = await call(client, world.courier_user, "reportOrderIssue", {**base, "issue_type": "boom"})
    assert bad.json() == {"error": "Invalid issue_type"}
    assert (
        await call(client, world.courier_user, "reportOrderIssue", {**base, "order_id": "x"})
    ).status_code == 404
    assert (await call(client, world.customer, "reportOrderIssue", base)).status_code == 403
    done = await world.order(status="delivered", courier=world.courier, fee="5")
    r = await call(client, world.courier_user, "reportOrderIssue", {**base, "order_id": str(done.id)})
    assert r.status_code == 409 and r.json() == {"error": "order_not_active"}


# --- getCustomerReliability ------------------------------------------------------------------------


async def incidents(world, count, days_ago=1):
    order = await world.order(status="cancelled")
    when = datetime.now(UTC) - timedelta(days=days_ago)
    async with SessionLocal() as s:
        for _ in range(count):
            s.add(
                NoResponseCase(
                    order_id=order.id,
                    status="resolved",
                    started_at=when,
                    deadline_at=when,
                    final_at=when,
                    incident_counted=True,
                )
            )
        await s.commit()


async def test_reliability(client, world, factory):
    await incidents(world, 1)
    await incidents(world, 3, days_ago=200)  # out of the window
    own = (await call(client, world.customer, "getCustomerReliability")).json()
    assert own["incidents"] == 1 and own["level"] == "notice" and own["window_days"] == 180
    order = await world.order()
    first = (
        await call(client, world.courier_user, "getCustomerReliability", {"order_id": str(order.id)})
    ).json()
    # couriers are warned from the first incident on (it was 2)
    assert first["incidents"] == 1 and first["level"] == "notice" and first["visible_to_couriers"] is True
    await incidents(world, 2)
    seen = (
        await call(client, world.courier_user, "getCustomerReliability", {"order_id": str(order.id)})
    ).json()
    assert (
        seen["level"] == "limited" and seen["max_advance_tnd"] == 30 and seen["phone_confirmation_required"]
    )
    stranger = await factory.user(email="s@example.test")
    assert (
        await call(client, stranger, "getCustomerReliability", {"order_id": str(order.id)})
    ).status_code == 403
    pending_user = await factory.user(email="p@example.test", profile=False)
    await world.make_courier(pending_user, verification="pending")
    assert (
        await call(client, pending_user, "getCustomerReliability", {"order_id": str(order.id)})
    ).status_code == 403
    assert (await call(client, world.admin, "getCustomerReliability", {"order_id": str(order.id)})).json()[
        "incidents"
    ] == 3
    assert (
        await call(client, world.customer, "getCustomerReliability", {"order_id": "x"})
    ).status_code == 404
    await incidents(world, 2)
    assert (await call(client, world.customer, "getCustomerReliability")).json()["suspended"] is True


# --- getCourierIdPhotos ------------------------------------------------------------------------------


async def test_id_photos_for_admins_only(client, world, factory):
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user, id_document_key="private/courier_id/x/doc.jpg")
    r = await call(
        client,
        world.admin,
        "getCourierIdPhotos",
        {"courier_ids": [str(other.id), str(world.courier.id), str(other.id), "bad", 5]},
    )
    assert r.status_code == 200
    photos = r.json()["photos"]
    assert list(photos) == [str(other.id)]
    assert photos[str(other.id)]["private"] is True and "X-Amz-Expires=300" in photos[str(other.id)]["url"]
    assert "private/courier_id/x/doc.jpg" not in r.text.replace(photos[str(other.id)]["url"], "")
    assert (
        await call(client, world.courier_user, "getCourierIdPhotos", {"courier_ids": []})
    ).status_code == 403
    assert (await call(client, world.admin, "getCourierIdPhotos", {})).status_code == 400
    assert (await call(client, world.admin, "getCourierIdPhotos", {"courier_ids": []})).json() == {
        "success": True,
        "photos": {},
    }

    # The admin screen knows which couriers to ask for (the key itself never leaves).
    listed = (await client.get("/api/entities/CourierProfile", headers=auth(world.admin))).json()
    flags = {p["id"]: p["has_id_photo"] for p in listed}
    assert flags == {str(other.id): True, str(world.courier.id): False}
    assert "private/courier_id/x/doc.jpg" not in json.dumps(listed)


def test_full_double_precision_from_a_real_phone_is_accepted():
    """Android WebViews report e.g. 35.825614699999995: it used to be refused as 'spoofed'."""
    from app.services.couriers import valid_coordinates

    assert valid_coordinates(35.825614699999995, 10.636912345678901) is None
