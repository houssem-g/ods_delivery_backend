"""proposeShop, the Shop / ShopReview / PlaceIndex compat entities and the retired DeliveryTariffs."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Shop, ShopMenuItem, ShopReview
from app.security.tokens import now_utc
from app.services.shops import build_shop, key_from_public_url, photo_url_of, stored_photo
from tests.catalog_data import TUNIS, add_place, add_shop
from tests.factories import auth, error_of

LAT, LNG = TUNIS
PROPOSE = "/api/functions/proposeShop"
PUBLIC = f"{settings.public_files_base_url}/public/review/2026/09/abc.jpg"


def proposal(**overrides):
    return {
        "name": "Chez Ali",
        "address": "12 rue de Marseille",
        "latitude": LAT,
        "longitude": LNG,
        **overrides,
    }


def q(value: dict) -> dict:
    return {"q": json.dumps(value)}


# --- proposeShop ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"name": " a "}, "invalid_name"),
        ({"address": "ab"}, "invalid_address"),
        ({"latitude": 48.85, "longitude": 2.35}, "invalid_location"),
        ({"latitude": None}, "invalid_location"),
        ({"latitude": "abc"}, "invalid_location"),
        ({"phone": "12345"}, "invalid_phone"),
        ({"category": "bar"}, "invalid_category"),
    ],
)
async def test_propose_shop_validation(client, factory, overrides, error):
    user = await factory.user()
    r = await client.post(PROPOSE, json=proposal(**overrides), headers=auth(user))
    assert r.status_code == 400 and r.json() == {"error": error}


def test_build_shop_normalizes():
    shop = build_shop(
        proposal(
            name="  Chez   Ali  ", phone="00216 22 123 456", category="bakery", opening_hours="  8h-20h ",
            description="x" * 600, latitude="36.8",
            menu_items=[
                {"name": " Pain ", "price": "0.4567", "photo_url": "https://cdn.example/p.jpg",
                 "description": "d"},
                {"name": "Gratuit", "price": 0},
                {"name": "Sans prix"},
                {"name": "Trop cher", "price": 10001},
                {"name": "", "price": 1},
                {"name": "Photo http", "price": 1, "photo_url": "http://example/p.jpg"},
                "not an object",
            ] + [{"name": f"item {i}", "price": 1} for i in range(40)],
        )
    )  # fmt: skip
    assert shop["name"] == "Chez Ali" and shop["phone"] == "+21622123456" and shop["latitude"] == 36.8
    assert shop["categories"] == ["bakery"] and shop["opening_hours"] == "8h-20h"
    assert len(shop["description"]) == 500
    menu = shop["menu_items"]
    assert menu[0] == {
        "name": "Pain",
        "price": 0.457,
        "photo_key": "https://cdn.example/p.jpg",
        "description": "d",
    }
    assert menu[1] == {"name": "Gratuit", "price": 0} and menu[2] == {"name": "Photo http", "price": 1}
    assert len(menu) == 3 + (30 - 7)  # only the first 30 entries are considered
    assert build_shop(proposal(phone="+216 71 123 456"))["phone"] == "+21671123456"


def test_public_url_helpers():
    assert key_from_public_url(PUBLIC) == "public/review/2026/09/abc.jpg"
    assert key_from_public_url("https://evil.example/public/x.jpg") is None
    assert key_from_public_url(f"{settings.public_files_base_url}/private/x.jpg") is None
    assert key_from_public_url(f"{settings.public_files_base_url}/public/../private/x") is None
    assert key_from_public_url(None) is None
    assert stored_photo(PUBLIC) == "public/review/2026/09/abc.jpg" and stored_photo("ftp://x") is None
    assert photo_url_of("public/a.jpg") == f"{settings.public_files_base_url}/public/a.jpg"
    assert photo_url_of("https://cdn/x.jpg") == "https://cdn/x.jpg" and photo_url_of(None) is None


async def test_propose_shop_creates_pending_with_menu(client, factory):
    user = await factory.user()
    assert (await client.post(PROPOSE, json=proposal())).status_code == 401
    r = await client.post(
        PROPOSE,
        json=proposal(category="restaurant", phone="22 123 456",
                      menu_items=[{"name": "Lablabi", "price": 3.5, "photo_url": PUBLIC}]),
        headers=auth(user),
    )  # fmt: skip
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["shop"]["name"] == "Chez Ali"
    assert body["shop"]["review_status"] == "pending" and "duplicate" not in body
    async with SessionLocal() as s:
        shop = await s.get(Shop, body["shop"]["id"])
        items = (await s.execute(select(ShopMenuItem))).scalars().all()
    assert shop.proposed_by == user.id and shop.osm_id.startswith("custom_") and shop.phone == "+21622123456"
    assert shop.categories == ["restaurant"] and shop.proposed_at is not None
    assert [(i.name, float(i.price), i.photo_key) for i in items] == [
        ("Lablabi", 3.5, "public/review/2026/09/abc.jpg")
    ]


async def test_propose_shop_duplicate_and_daily_limit(client, factory):
    user, admin = await factory.user(), await factory.user(role="admin")
    first = (await client.post(PROPOSE, json=proposal(), headers=auth(user))).json()
    twin = await client.post(
        PROPOSE, json=proposal(name="CHEZ ALI!", latitude=LAT + 0.001), headers=auth(user)
    )  # ~110 m away, same compacted name
    assert twin.json() == {
        "success": True, "duplicate": True,
        "shop": {"id": first["shop"]["id"], "name": "Chez Ali", "review_status": "pending"},
    }  # fmt: skip
    far = await client.post(PROPOSE, json=proposal(latitude=LAT + 0.01), headers=auth(user))
    assert "duplicate" not in far.json()
    await client.post(PROPOSE, json=proposal(name="Troisième"), headers=auth(user))
    limited = await client.post(PROPOSE, json=proposal(name="Quatrième"), headers=auth(user))
    assert limited.status_code == 429
    assert limited.json()["error"] == "too_many_proposals" and limited.json()["max"] == 3
    assert "rate limit" not in json.dumps(limited.json()).lower()
    # older than 24 h: no longer counted
    async with SessionLocal() as s:
        await s.execute(update(Shop).values(proposed_at=now_utc() - timedelta(hours=25)))
        await s.commit()
    assert (
        await client.post(PROPOSE, json=proposal(name="Cinquième"), headers=auth(user))
    ).status_code == 200
    # admins have no daily limit; a rejected twin does not count as a duplicate
    async with SessionLocal() as s:
        await s.execute(update(Shop).values(review_status="rejected", proposed_at=now_utc()))
        await s.commit()
    for name in ("A1", "A2", "A3", "A4"):
        r = await client.post(PROPOSE, json=proposal(name=name), headers=auth(admin))
        assert r.status_code == 200 and "duplicate" not in r.json()
    again = await client.post(PROPOSE, json=proposal(), headers=auth(admin))
    assert "duplicate" not in again.json()


# --- Shop entity --------------------------------------------------------------------------------


async def test_shop_read_policy_and_fields(client, factory):
    author, other, admin = await factory.user(), await factory.user(), await factory.user(role="admin")
    approved = await add_shop("Approved", LAT, LNG, categories=["restaurant"], photo_key="public/shop/a.jpg")
    pending = await add_shop("Pending", LAT, LNG, review_status="pending", proposed_by=author)
    await add_shop("Rejected", LAT, LNG, review_status="rejected", proposed_by=author)
    async with SessionLocal() as s:
        s.add_all([
            ShopMenuItem(shop_id=approved.id, name="B", price=2, position=1),
            ShopMenuItem(shop_id=approved.id, name="A", price=1.5, position=0, photo_key="public/menu/x.jpg"),
        ])  # fmt: skip
        await s.commit()

    async def names(user, query=None):
        r = await client.get("/api/entities/Shop", params=q(query) if query else None, headers=auth(user))
        assert r.status_code == 200
        return sorted(d["name"] for d in r.json())

    assert await names(other) == ["Approved"]
    assert await names(author) == ["Approved", "Pending", "Rejected"]
    assert await names(admin) == ["Approved", "Pending", "Rejected"]
    assert await names(admin, {"review_status": "pending"}) == ["Pending"]
    assert await names(other, {"review_status": "pending"}) == []

    doc = (await client.get(f"/api/entities/Shop/{approved.id}", headers=auth(other))).json()
    assert doc["latitude"] == pytest.approx(LAT) and doc["longitude"] == pytest.approx(LNG)
    assert doc["categories"] == ["restaurant"] and doc["review_status"] == "approved"
    assert doc["photo_url"] == f"{settings.public_files_base_url}/public/shop/a.jpg"
    assert doc["menu_items"] == [
        {"name": "A", "price": 1.5, "photo_url": f"{settings.public_files_base_url}/public/menu/x.jpg",
         "description": None},
        {"name": "B", "price": 2, "photo_url": None, "description": None},
    ]  # fmt: skip
    assert (await client.get(f"/api/entities/Shop/{pending.id}", headers=auth(other))).status_code == 404
    mine = (await client.get(f"/api/entities/Shop/{pending.id}", headers=auth(author))).json()
    assert mine["proposed_by"] == author.email and mine["menu_items"] == []
    as_admin = (await client.get(f"/api/entities/Shop/{pending.id}", headers=auth(admin))).json()
    assert as_admin["proposed_by"] == author.email
    # the author's e-mail can't be probed through a filter by someone else
    probe = await client.get(
        "/api/entities/Shop", params=q({"proposed_by": author.email}), headers=auth(other)
    )
    assert probe.json() == []


async def test_shop_admin_review_and_writes(client, factory):
    author, admin = await factory.user(), await factory.user(role="admin")
    pending = await add_shop("Pending", LAT, LNG, review_status="pending", proposed_by=author)
    url = f"/api/entities/Shop/{pending.id}"
    denied = await client.patch(url, json={"review_status": "approved"}, headers=auth(author))
    assert denied.status_code == 403 and error_of(denied) == "permission_denied"
    created = await client.post("/api/entities/Shop", json={"name": "X"}, headers=auth(admin))
    assert created.status_code == 403
    bad = await client.patch(url, json={"review_status": "maybe"}, headers=auth(admin))
    assert bad.status_code == 400
    short = await client.patch(url, json={"name": "x"}, headers=auth(admin))
    assert short.status_code == 400
    ok = await client.patch(
        url, json={"review_status": "approved", "phone": " ", "description": " Nouveau ", "id": "ignored"},
        headers=auth(admin),
    )  # fmt: skip
    assert ok.status_code == 200 and ok.json()["review_status"] == "approved"
    assert ok.json()["description"] == "Nouveau" and ok.json()["phone"] is None
    async with SessionLocal() as s:
        shop = await s.get(Shop, pending.id)
    assert shop.reviewed_by == admin.id and shop.reviewed_at is not None
    missing = await client.patch("/api/entities/Shop/not-a-uuid", json={}, headers=auth(admin))
    assert missing.status_code == 404
    assert (await client.delete(url, headers=auth(author))).status_code == 403
    assert (await client.delete(url, headers=auth(admin))).status_code in (200, 204)
    assert (await client.get(url, headers=auth(admin))).status_code == 404


# --- ShopReview ---------------------------------------------------------------------------------


async def test_review_create_and_read_by_key(client, factory):
    user, other = await factory.user(full_name="Sami B"), await factory.user()
    shop = await add_shop("Chez Ali", LAT, LNG)
    body = {
        "shop_osm_id": f"shop:{shop.id}", "shop_name": "ignored", "user_id": str(user.id),
        "user_name": "Fake Name", "rating": 4, "comment": " Très bon ", "photo_urls": [PUBLIC],
    }  # fmt: skip
    r = await client.post("/api/entities/ShopReview", json=body, headers=auth(user))
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["shop_osm_id"] == f"shop:{shop.id}" and doc["shop_name"] == "Chez Ali"
    assert doc["user_id"] == str(user.id) and doc["user_name"] == "Sami B" and doc["rating"] == 4
    assert doc["comment"] == "Très bon" and doc["photo_urls"] == [PUBLIC] and doc["created_by"] is None

    listed = await client.get(
        "/api/entities/ShopReview", params={**q({"shop_osm_id": f"shop:{shop.id}"}), "sort": "-created_date"},
        headers=auth(other),
    )  # fmt: skip
    assert [d["id"] for d in listed.json()] == [doc["id"]]
    again = await client.post("/api/entities/ShopReview", json=body, headers=auth(user))
    assert again.status_code == 409 and error_of(again) == "already_reviewed"


async def test_review_key_resolution(client, factory):
    user = await factory.user()
    place = await add_place("Café  de Paris", 36.8001, 10.1801, osm_id="node/42")
    osm_shop = await add_shop("OSM Shop", LAT, LNG, osm_id="custom_1")

    async def review(key, **extra):
        r = await client.post(
            "/api/entities/ShopReview", json={"shop_osm_id": key, "rating": 5, **extra}, headers=auth(user)
        )
        return r

    by_name = await review("place:café de paris@36.8001,10.1801")
    assert by_name.status_code == 201 and by_name.json()["shop_name"] == "Café  de Paris"
    by_osm = await review("node/42")  # same place through another key: one review per place
    assert by_osm.status_code == 409
    shop_by_osm = await review("custom_1")
    assert shop_by_osm.json()["shop_name"] == "OSM Shop"
    unknown = await review("place:inconnu@36.9,10.2")
    assert unknown.status_code == 201 and unknown.json()["shop_name"] is None
    async with SessionLocal() as s:
        rows = {r.target_key: r for r in (await s.execute(select(ShopReview))).scalars()}
    assert rows["place:café de paris@36.8001,10.1801"].place_id == place.id
    assert rows["custom_1"].shop_id == osm_shop.id
    assert (
        rows["place:inconnu@36.9,10.2"].shop_id is None and rows["place:inconnu@36.9,10.2"].place_id is None
    )


async def test_review_refusals(client, factory):
    user, other = await factory.user(), await factory.user()
    hidden = await add_shop("Pending", LAT, LNG, review_status="pending", proposed_by=other)
    url = "/api/entities/ShopReview"

    async def create(**body):
        return await client.post(url, json={"shop_osm_id": "node/1", "rating": 3, **body}, headers=auth(user))

    assert (await client.post(url, json={"shop_osm_id": "node/1", "rating": 3})).status_code == 401
    assert (await create(user_id=str(other.id))).status_code == 403
    assert (await create(user_id="not-an-id")).status_code == 403
    for rating in (0, 6, 4.5, None, "5"):
        r = await create(rating=rating)
        assert r.status_code == 400, rating
    assert (await create(shop_osm_id="  ")).status_code == 400
    assert (await create(shop_osm_id="x" * 301)).status_code == 400
    assert (await create(photo_urls=["https://evil.example/x.jpg"])).status_code == 400
    assert (await create(photo_urls=[PUBLIC] * 11)).status_code == 400
    assert (await create(shop_osm_id=f"shop:{hidden.id}")).status_code == 404
    assert (await create(shop_osm_id="shop:not-a-uuid")).status_code == 404
    assert (await create(rating=4.0, user_id=str(user.id))).status_code == 201


async def test_review_delete_and_update(client, factory):
    author, other, admin = await factory.user(), await factory.user(), await factory.user(role="admin")
    url = "/api/entities/ShopReview"
    first = (await client.post(url, json={"shop_osm_id": "k1", "rating": 2}, headers=auth(author))).json()
    second = (await client.post(url, json={"shop_osm_id": "k2", "rating": 2}, headers=auth(author))).json()
    assert (
        await client.patch(f"{url}/{first['id']}", json={"rating": 5}, headers=auth(admin))
    ).status_code == 403
    assert (await client.delete(f"{url}/{first['id']}", headers=auth(other))).status_code == 403
    assert (await client.delete(f"{url}/{first['id']}", headers=auth(author))).status_code in (200, 204)
    assert (await client.delete(f"{url}/{second['id']}", headers=auth(admin))).status_code in (200, 204)
    assert (await client.delete(f"{url}/{second['id']}", headers=auth(admin))).status_code == 404
    assert (await client.delete(f"{url}/nope", headers=auth(admin))).status_code == 404


# --- PlaceIndex, DeliveryTariffs ----------------------------------------------------------------


async def test_place_index_admin_read_only(client, factory):
    user, admin = await factory.user(), await factory.user(role="admin")
    place = await add_place("Pharmacie", LAT, LNG, category="pharmacie", quality=75)
    assert (await client.get("/api/entities/PlaceIndex", headers=auth(user))).json() == []
    docs = (
        await client.get("/api/entities/PlaceIndex", params=q({"category": "pharmacie"}), headers=auth(admin))
    ).json()
    assert len(docs) == 1 and docs[0]["id"] == str(place.id) and docs[0]["quality_score"] == 0.75
    assert docs[0]["lat"] == pytest.approx(LAT) and docs[0]["osm_id"] == place.osm_id
    one = await client.get(f"/api/entities/PlaceIndex/{place.id}", headers=auth(admin))
    assert one.status_code == 200 and one.json()["name"] == "Pharmacie"
    denied = await client.post("/api/entities/PlaceIndex", json={"name": "x"}, headers=auth(admin))
    assert denied.status_code == 403


async def test_delivery_tariffs_is_a_retired_stub(client, factory):
    admin = await factory.user(role="admin")
    listed = await client.get("/api/entities/DeliveryTariffs", headers=auth(admin))
    assert listed.status_code == 200 and listed.json() == []
    filtered = await client.get(
        "/api/entities/DeliveryTariffs", params=q({"is_active": True}), headers=auth(admin)
    )
    assert filtered.json() == []
    assert (await client.get("/api/entities/DeliveryTariffs/abc", headers=auth(admin))).status_code == 404
    for response in (
        await client.post(
            "/api/entities/DeliveryTariffs", json={"name": "Std", "price_per_km": 1}, headers=auth(admin)
        ),
        await client.patch(
            "/api/entities/DeliveryTariffs/abc", json={"is_active": False}, headers=auth(admin)
        ),
        await client.delete("/api/entities/DeliveryTariffs/abc", headers=auth(admin)),
    ):
        assert response.status_code == 410 and error_of(response) == "retired"
