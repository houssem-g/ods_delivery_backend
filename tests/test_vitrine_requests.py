"""« Mettez votre carte sur ODS Delivery » (17/10): a restaurant's request, the admin's inbox, the
acceptance through createVitrine, anti-abuse and privacy. Rules: app/services/vitrine_requests.py."""

import uuid

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import File, Notification, VitrineRequest
from app.rate_limit import limiter
from app.services.push import push_link
from tests.order_helpers import SOUSSE_SHOP, OrderWorld, rows
from tests.partner_helpers import make_partner
from tests.vitrine_helpers import QA_CUSTOMER, fn, manual_vitrine, ok, shop_body


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


def request_body(**extra):
    return {
        "shop_name": "Pizzeria Amel",
        "phone": "22 333 444",
        "address": "Khezama Est, Sousse",
        "kind": "restaurant",
        "uses_ods": False,
        "message": "Nous faisons des pizzas au feu de bois.",
        "website": "",
        **extra,
    }


async def count_requests() -> int:
    async with SessionLocal() as s:
        return int((await s.execute(select(func.count(VitrineRequest.id)))).scalar_one())


async def private_menu_file(owner_id: uuid.UUID, purpose: str = "menu") -> str:
    key = f"private/{purpose}/{owner_id}/{uuid.uuid4().hex}.jpg"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key,
                owner_id=owner_id,
                visibility="private",
                purpose=purpose,
                content_type="image/jpeg",
                size_bytes=1000,
            )
        )
        await s.commit()
    return key


async def test_public_request_without_account_then_followed_with_its_token(client, world):
    body = await ok(
        client, None, "submitVitrineRequest", request_body(lat=SOUSSE_SHOP[0], lng=SOUSSE_SHOP[1])
    )
    assert body["request"]["status"] == "pending"
    assert body["request"]["source"] == "public"
    token = body["follow_token"]
    assert len(token) >= 40
    row = (await rows(select(VitrineRequest)))[0]
    assert row.phone == "+21622333444"
    assert row.follow_token_hash != token and row.client_hash  # only hashes are stored
    assert row.test is False and row.user_id is None

    # the device follows it with the token: its own data only, never the phone / the address
    status = await ok(client, None, "getVitrineRequestStatus", {"token": token})
    assert status["request"]["status"] == "pending"
    assert status["request"]["shop_name"] == "Pizzeria Amel"
    assert "phone" not in status["request"] and "address" not in status["request"]
    r = await fn(client, None, "getVitrineRequestStatus", {"token": "x" * 43})
    assert r.status_code == 404 and r.json()["error"] == "not_found"

    # the admin's inbox: everything, with the counter
    inbox = await ok(client, world.admin, "listVitrineRequests", {})
    assert inbox["counts"] == {"pending": 1, "accepted": 0, "refused": 0}
    seen = inbox["requests"][0]
    assert seen["phone"] == "+21622333444" and seen["address"] == "Khezama Est, Sousse"
    assert seen["lat"] == pytest.approx(SOUSSE_SHOP[0]) and seen["kind"] == "restaurant"
    assert seen["message"].startswith("Nous faisons")

    # nobody else reads the inbox
    for user in (world.customer, None):
        r = await fn(client, user, "listVitrineRequests", {})
        assert r.status_code in (401, 403)


async def test_honeypot_and_validation(client, world):
    fake = await ok(client, None, "submitVitrineRequest", request_body(website="http://spam.example"))
    assert fake == {"success": True, "request": {"status": "pending"}}
    assert await count_requests() == 0
    for bad, code in (
        ({"phone": "123"}, "invalid_phone"),
        ({"phone": "+33612345678"}, "invalid_phone"),
        ({"shop_name": "A"}, "invalid_name"),
        ({"address": ""}, "invalid_address"),
        ({"lat": 48.85, "lng": 2.35}, "invalid_location"),
        ({"lat": 35.8}, "invalid_location"),
        ({"kind": "casino"}, "invalid_kind"),
    ):
        limiter.reset()  # every attempt counts in the per-address limit, valid or not
        r = await fn(client, world.customer, "submitVitrineRequest", request_body(**bad))
        assert r.status_code == 400 and r.json()["error"] == code, (bad, r.text)
    assert await count_requests() == 0


async def test_one_open_request_per_phone_without_revealing_it(client, world, factory):
    mine = await ok(client, world.customer, "submitVitrineRequest", request_body())
    assert mine["request"]["source"] == "app"
    # the same person again: his own request, no second one
    again = await ok(client, world.customer, "submitVitrineRequest", request_body(shop_name="Autre nom"))
    assert again["already_pending"] is True and again["request"]["id"] == mine["request"]["id"]
    # somebody else (or nobody) with the same number: the same generic success, nothing revealed
    other = await factory.user(email="other@example.test")
    for user in (other, None):
        body = await ok(client, user, "submitVitrineRequest", request_body())
        assert body == {"success": True, "request": {"status": "pending"}}
    assert await count_requests() == 1
    # the requester sees his request in « Ma carte »
    listed = await ok(client, world.customer, "listMyVitrineRequests", {})
    assert [r["id"] for r in listed["requests"]] == [mine["request"]["id"]]
    assert "phone" not in listed["requests"][0]
    assert (await ok(client, other, "listMyVitrineRequests", {}))["requests"] == []


async def test_limits_per_address(client, world):
    for i in range(5):
        await ok(client, None, "submitVitrineRequest", request_body(phone=f"2233344{i}"))
    r = await fn(client, None, "submitVitrineRequest", request_body(phone="22333449"))
    assert r.status_code == 429 and r.json()["error"] == "too_many_requests"
    assert await count_requests() == 5


async def test_photo_of_the_menu_is_a_private_upload_of_the_requester(client, world, factory):
    key = await private_menu_file(world.customer.id)
    # without an account: no photo
    r = await fn(client, None, "submitVitrineRequest", request_body(menu_photo_key=key))
    assert r.status_code == 400 and r.json()["error"] == "invalid_photo"
    # somebody else's file, a public one, another purpose: refused
    other = await factory.user(email="other@example.test")
    r = await fn(client, other, "submitVitrineRequest", request_body(menu_photo_key=key))
    assert r.json()["error"] == "invalid_photo"
    r = await fn(
        client,
        world.customer,
        "submitVitrineRequest",
        request_body(menu_photo_key=await private_menu_file(world.customer.id, "chat")),
    )
    assert r.json()["error"] == "invalid_photo"
    await ok(client, world.customer, "submitVitrineRequest", request_body(menu_photo_key=key))
    seen = (await ok(client, world.admin, "listVitrineRequests", {}))["requests"][0]
    assert seen["menu_photo_url"] and key in seen["menu_photo_url"]
    assert seen["account"]["email"] == world.customer.email
    mine = (await ok(client, world.customer, "listMyVitrineRequests", {}))["requests"][0]
    assert "menu_photo_url" not in mine


async def test_refuse_with_a_reason_the_requester_reads(client, world):
    sent = await ok(client, world.customer, "submitVitrineRequest", request_body())
    rid = sent["request"]["id"]
    r = await fn(client, world.admin, "refuseVitrineRequest", {"request_id": rid, "reason": ""})
    assert r.status_code == 400
    r = await fn(client, world.customer, "refuseVitrineRequest", {"request_id": rid, "reason": "Hors zone"})
    assert r.status_code == 403
    body = await ok(
        client,
        world.admin,
        "refuseVitrineRequest",
        {"request_id": rid, "reason": "Hors de Sousse pour l'instant"},
    )
    assert body["request"]["status"] == "refused"
    mine = (await ok(client, world.customer, "listMyVitrineRequests", {}))["requests"][0]
    assert mine["status"] == "refused" and mine["refuse_reason"] == "Hors de Sousse pour l'instant"
    notes = await rows(select(Notification).where(Notification.user_id == world.customer.id))
    assert [n.type for n in notes] == ["vitrine_request"]
    assert "Hors de Sousse" in notes[0].body_fr and notes[0].data["status"] == "refused"
    # decided once
    r = await fn(client, world.admin, "refuseVitrineRequest", {"request_id": rid, "reason": "encore"})
    assert r.status_code == 409 and r.json()["error"] == "request_closed"
    # a refused number may ask again (not an open request any more)
    again = await ok(client, world.customer, "submitVitrineRequest", request_body())
    assert again["request"]["id"] != rid


async def test_accept_creates_the_vitrine_and_points_to_it(client, world):
    sent = await ok(client, world.customer, "submitVitrineRequest", request_body())
    rid = sent["request"]["id"]
    created = await ok(
        client,
        world.admin,
        "createVitrine",
        {"shop": shop_body("Pizzeria Amel", category="restaurant"), "source": "manual", "request_id": rid},
    )
    shop_id = created["vitrine"]["shop_id"]
    row = (await rows(select(VitrineRequest)))[0]
    assert row.status == "accepted" and str(row.shop_id) == shop_id and row.decided_by == world.admin.id
    mine = (await ok(client, world.customer, "listMyVitrineRequests", {}))["requests"][0]
    assert mine["status"] == "accepted" and mine["shop"]["id"] == shop_id and mine["shop"]["slug"]
    note = (await rows(select(Notification).where(Notification.user_id == world.customer.id)))[0]
    assert note.type == "vitrine_request" and note.data["status"] == "accepted"
    assert push_link("vitrine_request", None, note.data) == "/ma-carte?as=customer"
    # a decided request cannot create a second Vitrine: refused before anything is written
    r = await fn(
        client,
        world.admin,
        "createVitrine",
        {"shop": shop_body("Deuxième", at=(35.83, 10.60)), "source": "manual", "request_id": rid},
    )
    assert r.status_code == 409 and r.json()["error"] == "request_closed"
    assert len((await ok(client, world.admin, "listVitrines", {}))["vitrines"]) == 1
    r = await fn(
        client, world.admin, "createVitrine", {"shop": shop_body("X"), "request_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404 and r.json()["error"] == "request_not_found"


async def test_request_sent_by_ods_and_linked_to_an_existing_vitrine(client, world):
    pw = await make_partner(client, world.admin, ods_restaurant_id=1)
    body = {
        "source": "ods",
        "ods_restaurant_id": 1,
        "test": True,
        "shop_name": "Coffee and Tea",
        "phone": "73123456",
        "address": "Av. Habib Bourguiba, Sousse",
        "lat": SOUSSE_SHOP[0],
        "lng": SOUSSE_SHOP[1],
        "kind": "cafe",
    }
    sent = await ok(client, None, "submitVitrineRequest", body)
    assert sent["request"]["source"] == "ods" and sent["follow_token"]
    twice = await ok(client, None, "submitVitrineRequest", {**body, "phone": "73123457"})
    assert twice["already_pending"] is True and "follow_token" not in twice
    r = await fn(client, None, "submitVitrineRequest", {**body, "ods_restaurant_id": "abc"})
    assert r.status_code == 400
    row = (await rows(select(VitrineRequest)))[0]
    assert row.test is True and row.uses_ods is True and row.ods_restaurant_id == 1 and row.user_id is None
    seen = (await ok(client, world.admin, "listVitrineRequests", {}))["requests"][0]
    assert seen["partner"]["id"] == str(pw.partner.id) and seen["partner"]["vitrine_shop_id"] is None
    shop_id, _ = await manual_vitrine(client, world.admin, name="Coffee and Tea")
    r = await fn(
        client, world.admin, "acceptVitrineRequest", {"request_id": seen["id"], "shop_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404 and r.json()["error"] == "vitrine_not_found"
    done = await ok(
        client, world.admin, "acceptVitrineRequest", {"request_id": seen["id"], "shop_id": shop_id}
    )
    assert done["request"]["status"] == "accepted" and done["request"]["shop"]["id"] == shop_id
    status = await ok(client, None, "getVitrineRequestStatus", {"token": sent["follow_token"]})
    assert status["request"]["status"] == "accepted"


async def test_a_qa_account_request_is_a_test(client, factory):
    qa = await factory.user(email=QA_CUSTOMER)
    await ok(client, qa, "submitVitrineRequest", request_body(test=False))
    assert (await rows(select(VitrineRequest)))[0].test is True


async def test_nearby_list_carries_the_page_fields(client, world, factory):
    qa = await factory.user(email=QA_CUSTOMER)
    await manual_vitrine(client, world.admin, name="Chez Amel")
    body = await ok(client, qa, "listVitrinesNearby", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]})
    v = body["vitrines"][0]
    assert v["address"] == "Av. Habib Bourguiba, Sousse" and v["city"] == "Sousse"
    assert "photo_url" in v and "closes_at" in v and len(v["preview"]) == 2
    # a normal account still sees nothing (QA Vitrine)
    assert (
        await ok(client, world.customer, "listVitrinesNearby", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]})
    )["vitrines"] == []
