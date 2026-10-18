"""Vitrine (étape 8): admin functions, visibility (QA / public switch), the ODS catalog push, the
merchant's private link, hours. Contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.errors import ApiError
from app.jobs.registry import JOBS
from app.models import Order, PartnerCatalog, Shop, ShopMenuItem, Vitrine, VitrineProduct, VitrineVisit
from app.services import vitrine as vitrine_service
from app.services import vitrine_stats
from tests.factories import auth
from tests.order_helpers import SOUSSE_SHOP, OrderWorld, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, WebhookServer, make_partner
from tests.vitrine_helpers import (
    ALWAYS_OPEN,
    QA_CUSTOMER,
    cart_order,
    catalog_body,
    fn,
    manual_vitrine,
    ok,
    shop_body,
)

BOX = {"minLat": 35.80, "maxLat": 35.85, "minLng": 10.58, "maxLng": 10.63}


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
async def qa(factory):
    return await factory.user(email=QA_CUSTOMER, full_name="Lemel QA", phone_e164="+21622111333")


async def put_catalog(client, pw, body):
    return await client.put(f"{PARTNER_API}/catalog", json=body, headers=pw.headers)


async def products_of(shop_id) -> list[VitrineProduct]:
    return await rows(
        select(VitrineProduct).where(VitrineProduct.shop_id == shop_id).order_by(VitrineProduct.position)
    )


async def switch(client, admin, on: bool) -> None:
    await ok(client, admin, "setVitrineSettings", {"public_allowed": on})


# --- units ------------------------------------------------------------------------------------------


def test_hours_osm_text_and_open_state():
    hours = vitrine_service.clean_hours(
        {
            "weekly": [
                {"days": [1, 2, 3, 4, 5], "open": "08:00", "close": "12:00"},
                {"days": [1, 2, 3, 4, 5], "open": "14:00", "close": "19:00"},
                {"days": [6], "open": "09:00", "close": "13:00"},
            ]
        }
    )
    assert vitrine_service.osm_opening_hours(hours) == "Mo-Fr 08:00-12:00,14:00-19:00; Sa 09:00-13:00"
    assert vitrine_service.osm_opening_hours(ALWAYS_OPEN) == "Mo-Su 00:00-24:00"
    # Monday 16/10/2026 is a Friday: 10:00 Tunis = 09:00 UTC
    friday_10 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
    assert vitrine_service.open_state(hours, friday_10) == {
        "open_now": True,
        "opens_at": None,
        "closes_at": "12:00",
    }
    friday_13 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert vitrine_service.open_state(hours, friday_13) == {
        "open_now": False,
        "opens_at": "14:00",
        "closes_at": None,
    }
    sunday = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    assert vitrine_service.open_state(hours, sunday)["opens_at"] == "08:00"
    # past midnight: Saturday 22:00-02:00 is still open on Sunday 01:00
    late = vitrine_service.clean_hours({"weekly": [{"days": [6], "open": "22:00", "close": "02:00"}]})
    assert vitrine_service.open_state(late, datetime(2026, 10, 18, 0, 0, tzinfo=UTC))["open_now"] is True
    assert vitrine_service.open_state(None) == {"open_now": None, "opens_at": None, "closes_at": None}
    # integration 17/10: a shop open all day every day never « closes at 00:00 »; a range ending at
    # 24:00 followed by one starting at 00:00 closes at the end of the second one
    assert vitrine_service.open_state(ALWAYS_OPEN, friday_10) == {
        "open_now": True,
        "opens_at": None,
        "closes_at": None,
    }
    assert (
        vitrine_service.open_state(ALWAYS_OPEN, datetime(2026, 10, 18, 22, 59, tzinfo=UTC))["closes_at"]
        is None
    )
    chained = vitrine_service.clean_hours(
        {
            "weekly": [
                {"days": [5], "open": "20:00", "close": "24:00"},
                {"days": [6], "open": "00:00", "close": "02:00"},
            ]
        }
    )
    assert (
        vitrine_service.open_state(chained, datetime(2026, 10, 16, 21, 0, tzinfo=UTC))["closes_at"] == "02:00"
    )
    sunday_night = vitrine_service.clean_hours(
        {
            "weekly": [
                {"days": [7], "open": "22:00", "close": "24:00"},
                {"days": [1], "open": "00:00", "close": "03:00"},
            ]
        }
    )
    assert (
        vitrine_service.open_state(sunday_night, datetime(2026, 10, 18, 22, 0, tzinfo=UTC))["closes_at"]
        == "03:00"
    )
    assert (
        vitrine_service.open_state(sunday_night, datetime(2026, 10, 19, 0, 30, tzinfo=UTC))["closes_at"]
        == "03:00"
    )
    with pytest.raises(ApiError):
        vitrine_service.clean_hours({"weekly": [{"days": [8], "open": "08:00", "close": "09:00"}]})


def test_slugify_and_purge_job_registered():
    assert vitrine_service.slugify("Coffee & Tea — Khezama") == "coffee-tea-khezama"
    assert vitrine_service.slugify("مقهى") == ""
    assert JOBS["purge_vitrine_visits"].enabled


# --- admin ------------------------------------------------------------------------------------------


async def test_admin_only(client, world):
    for name in (
        "listVitrines",
        "createVitrine",
        "setVitrineSettings",
        "getVitrineSettings",
        "listPartnerCatalogs",
    ):
        r = await fn(client, world.customer, name, {"public_allowed": True})
        assert r.status_code == 403 and r.json()["error"] == "forbidden", name
    assert (await fn(client, None, "listVitrines")).status_code == 401


async def test_create_manual_vitrine_and_its_catalog(client, world):
    admin = world.admin
    body = await ok(
        client, admin, "createVitrine", {"shop": shop_body("Coffee and Tea"), "hours": ALWAYS_OPEN}
    )
    v = body["vitrine"]
    assert v["status"] == "draft" and v["visibility"] == "qa" and v["source"] == "manual"
    assert v["slug"] == "coffee-and-tea-sousse" and v["merchant_link"] is None
    assert v["opening_hours"] == "Mo-Su 00:00-24:00" and v["open_now"] is True
    shop_id = v["shop_id"]
    cat = (await ok(client, admin, "upsertVitrineCategory", {"shop_id": shop_id, "name": "Boissons"}))[
        "category"
    ]
    p = await ok(
        client,
        admin,
        "upsertVitrineProduct",
        {"shop_id": shop_id, "name": "Jus d'orange 0,5 L", "price": 4.5, "category_id": cat["id"]},
    )
    product = p["product"]
    assert product["price"] == 4.5 and product["available"] is True and product["category_id"] == cat["id"]
    for bad in (-1, 2000.5, "abc", None):
        r = await fn(client, admin, "upsertVitrineProduct", {"shop_id": shop_id, "name": "X", "price": bad})
        assert r.status_code == 400 and r.json()["error"] == "validation_error"
    await ok(
        client, admin, "upsertVitrineProduct", {"shop_id": shop_id, "id": product["id"], "available": False}
    )
    await ok(client, admin, "deleteVitrineCategory", {"shop_id": shop_id, "id": cat["id"]})
    detail = await ok(client, admin, "getVitrineAdmin", {"shop_id": shop_id})
    assert detail["categories"] == []
    assert detail["products"][0]["available"] is False and detail["products"][0]["category_id"] is None
    await ok(client, admin, "deleteVitrineProduct", {"shop_id": shop_id, "id": product["id"]})
    assert (await ok(client, admin, "getVitrineAdmin", {"shop_id": shop_id}))["products"] == []
    assert len(await products_of(shop_id)) == 1  # archived, never deleted
    # the same name within 150 m: transform the existing shop, never a duplicate
    again = await fn(client, admin, "createVitrine", {"shop": shop_body("Coffee and Tea")})
    assert again.status_code == 409 and again.json()["error"] == "shop_exists"
    assert again.json()["shop_id"] == shop_id
    twice = await fn(client, admin, "createVitrine", {"shop_id": shop_id})
    assert twice.status_code == 409 and twice.json()["error"] == "vitrine_exists"
    # a shop with a Vitrine is never deleted physically
    r = await client.delete(f"/api/entities/Shop/{shop_id}", headers=auth(admin))
    assert r.status_code == 409 and r.json()["error"] == "vitrine_exists"


async def test_a_customer_proposal_becomes_a_vitrine_with_its_menu(client, world):
    r = await fn(
        client,
        world.customer,
        "proposeShop",
        {
            "name": "Boulangerie Ali",
            "address": "Rue X, Sousse",
            "latitude": SOUSSE_SHOP[0],
            "longitude": SOUSSE_SHOP[1],
            "category": "bakery",
            "menu_items": [{"name": "Baguette", "price": 0.25}, {"name": "Croissant", "price": 0.8}],
        },
    )
    shop_id = r.json()["shop"]["id"]
    v = (await ok(client, world.admin, "createVitrine", {"shop_id": shop_id}))["vitrine"]
    assert v["counts"]["products"] == 2 and v["visibility"] == "qa"
    shop = await reload(Shop, shop_id)
    assert shop.review_status == "approved" and shop.visibility == "qa"
    assert len(await rows(select(ShopMenuItem))) == 2  # the proposal itself is kept


async def test_settings_switch(client, world):
    assert (await ok(client, world.admin, "getVitrineSettings"))["public_allowed"] is False
    assert (await ok(client, world.admin, "setVitrineSettings", {"public_allowed": True}))[
        "public_allowed"
    ] is True
    assert (await ok(client, world.admin, "getVitrineSettings"))["public_allowed"] is True
    r = await fn(client, world.admin, "setVitrineSettings", {"public_allowed": "yes"})
    assert r.status_code == 400


# --- visibility -------------------------------------------------------------------------------------


async def test_a_qa_vitrine_is_seen_by_qa_and_admins_only(client, world, qa):
    shop_id, _ = await manual_vitrine(client, world.admin)
    other = world.customer

    async def shop_names(user):
        r = await client.get("/api/entities/Shop", headers=auth(user))
        return [d["name"] for d in r.json()]

    async def bbox(user):
        r = await fn(client, user, "searchByBbox", BOX)
        return [row for row in r.json()["data"]["results"] if row["source"] == "shop"]

    # a non-QA customer: nowhere, and the page answers exactly like an unknown id
    assert await shop_names(other) == []
    assert await bbox(other) == []
    hidden = await fn(client, other, "getVitrine", {"shop_id": shop_id})
    unknown = await fn(client, other, "getVitrine", {"shop_id": "00000000-0000-0000-0000-000000000000"})
    assert hidden.status_code == unknown.status_code == 404 and hidden.json() == unknown.json()
    assert (await fn(client, other, "getVitrine", {"slug": "chez-amel-sousse"})).json() == unknown.json()
    assert (await fn(client, None, "getVitrine", {"shop_id": shop_id})).status_code == 404
    places = await fn(client, other, "searchPlaces", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]})
    assert all(p["name"] != "Chez Amel" for p in places.json().get("results", places.json().get("data", [])))
    near = await ok(client, other, "listVitrinesNearby", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]})
    assert near["vitrines"] == []
    assert (await client.get(f"/api/entities/Shop/{shop_id}", headers=auth(other))).status_code == 404

    # the QA account (lemelec346) and the admin see it, flagged « test »
    for user in (qa, world.admin):
        assert await shop_names(user) == ["Chez Amel"]
        [row] = await bbox(user)
        assert row["vitrine"] == {"slug": "chez-amel-sousse", "source": "manual", "test": True}
        page = (await ok(client, user, "getVitrine", {"slug": "chez-amel-sousse"}))["vitrine"]
        assert page["name"] == "Chez Amel" and page["test"] is True and len(page["products"]) == 2
        assert "ods_menu_id" not in page["products"][0]
    near = await ok(client, qa, "listVitrinesNearby", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]})
    assert [v["name"] for v in near["vitrines"]] == ["Chez Amel"] and len(near["vitrines"][0]["preview"]) == 2
    doc = (await client.get(f"/api/entities/Shop/{shop_id}", headers=auth(qa))).json()
    assert doc["vitrine"]["source"] == "manual" and doc["visibility"] == "qa"

    # paused: gone for the QA account too (« plus disponible »), still there for the admin
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "paused"})
    gone = await fn(client, qa, "getVitrine", {"shop_id": shop_id})
    assert gone.status_code == 404 and gone.json()["error"] == "vitrine_unavailable"
    assert await shop_names(qa) == []
    assert (await fn(client, other, "getVitrine", {"shop_id": shop_id})).json()["error"] == "not_found"
    assert await shop_names(world.admin) == ["Chez Amel"]


async def test_publishing_needs_the_switch_and_the_checklist(client, world):
    shop_id, _ = await manual_vitrine(client, world.admin, hours=None)
    other = world.customer

    async def publish():
        return await fn(
            client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"}
        )

    r = await publish()
    assert r.status_code == 409 and r.json()["error"] == "public_not_allowed"
    await switch(client, world.admin, True)
    r = await publish()
    assert r.status_code == 409 and r.json()["error"] == "checklist_incomplete"
    assert r.json()["missing"] == ["hours"]
    await ok(client, world.admin, "updateVitrine", {"shop_id": shop_id, "hours": ALWAYS_OPEN})
    v = await ok(client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"})
    assert v["vitrine"]["visibility"] == "public" and v["vitrine"]["can_go_public"] is True
    # now everybody sees it, without an account too (the QR code), not flagged « test »
    page = (await ok(client, None, "getVitrine", {"shop_id": shop_id}))["vitrine"]
    assert page["test"] is False
    rows_ = [row for row in (await fn(client, other, "searchByBbox", BOX)).json()["data"]["results"]]
    assert rows_[0]["shop_id"] == shop_id and rows_[0]["vitrine"]["test"] is False  # Vitrines first
    # the switch off is a kill switch: hidden again at once, nothing rewritten
    await switch(client, world.admin, False)
    assert (await fn(client, other, "getVitrine", {"shop_id": shop_id})).status_code == 404
    assert (await reload(Shop, shop_id)).visibility == "public"


# --- ODS catalog --------------------------------------------------------------------------------------


async def test_catalog_push_is_idempotent_and_upserts_by_ods_id(client, world, qa):
    pw = await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)
    assert (await client.put(f"{PARTNER_API}/catalog", json=catalog_body())).status_code == 401
    # before any Vitrine: kept, shown to the admin
    first = await put_catalog(client, pw, catalog_body())
    assert first.status_code == 200, first.text
    assert first.json() == {
        "status": "updated",
        "catalog_version": "sha256:v1",
        "products": 3,
        "vitrine": None,
    }
    listed = (await ok(client, world.admin, "listPartnerCatalogs"))["catalogs"]
    assert listed[0]["catalog"]["products"] == 3 and listed[0]["vitrine"] is None

    created = await ok(
        client,
        world.admin,
        "createVitrine",
        {"shop": shop_body("Coffee and Tea"), "source": "ods", "partner_id": str(pw.partner.id)},
    )
    shop_id = created["vitrine"]["shop_id"]
    assert created["vitrine"]["counts"] == {"categories": 1, "products": 3, "available_products": 2}
    assert created["vitrine"]["opening_hours"] == "Mo-Su 00:00-24:00"
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    before = {p.ods_menu_id: p.updated_at for p in await products_of(shop_id)}
    snapshot = await reload(PartnerCatalog, pw.partner.id)

    # same content (another version string, another date): unchanged, nothing written
    same = await put_catalog(
        client, pw, catalog_body(version="sha256:v1-bis", generated_at="2026-10-17T09:00:00Z")
    )
    # (vcc 18/10: ODS also gets slug / status / public for « Mon lien »)
    assert same.json()["status"] == "unchanged" and same.json()["vitrine"]["shop_id"] == shop_id
    assert set(same.json()["vitrine"]) == {"shop_id", "slug", "status", "public"}
    assert {p.ods_menu_id: p.updated_at for p in await products_of(shop_id)} == before
    again = await reload(PartnerCatalog, pw.partner.id)
    assert again.received_at == snapshot.received_at and again.content_hash == snapshot.content_hash

    # a price 2.5 → 2.8, a dish gone, a new one: visible at the next getVitrine
    products = catalog_body()["products"]
    products[0]["price"] = 2.8
    products = [products[0], products[2], {"ods_menu_id": 400, "name": "Cake", "price": 3, "position": 3}]
    changed = await put_catalog(client, pw, catalog_body(products, version="sha256:v2"))
    assert changed.json()["status"] == "updated" and changed.json()["products"] == 3
    by_id = {p.ods_menu_id: p for p in await products_of(shop_id)}
    assert by_id[346].archived_at is not None and by_id[400].archived_at is None
    page = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]
    prices = {p["name"]: p["price"] for p in page["products"]}
    assert prices == {"Café crème": 2.8, "Citronnade": 3.2, "Cake": 3.0}
    assert {p["name"]: p["available"] for p in page["products"]}["Citronnade"] is False
    assert page["source"] == "ods" and page["catalog_stale"] is False
    # the dish comes back: un-archived (same row)
    back = await put_catalog(client, pw, catalog_body(version="sha256:v3"))
    assert back.json()["status"] == "updated"
    by_id = {p.ods_menu_id: p for p in await products_of(shop_id)}
    assert by_id[346].archived_at is None and by_id[400].archived_at is not None
    # an ODS-fed catalog is locked for the admin
    r = await fn(client, world.admin, "upsertVitrineProduct", {"shop_id": shop_id, "name": "X", "price": 1})
    assert r.status_code == 409 and r.json()["error"] == "catalog_locked"


async def test_catalog_validation(client, world):
    pw = await make_partner(client, world.admin)
    many = [{"ods_menu_id": i + 1, "name": f"P{i}", "price": 1} for i in range(301)]
    r = await put_catalog(client, pw, catalog_body(many))
    assert r.status_code == 413 and r.json() == {**r.json(), "error": "too_many_products", "max": 300}
    r = await put_catalog(client, pw, catalog_body([{"ods_menu_id": 1, "name": "A", "price": 5000}]))
    assert r.status_code == 400 and r.json()["error"] == "validation_error"
    dup = [{"ods_menu_id": 1, "name": "A", "price": 1}, {"ods_menu_id": 1, "name": "B", "price": 1}]
    assert (await put_catalog(client, pw, catalog_body(dup))).status_code == 400
    r = await client.put(
        f"{PARTNER_API}/catalog",
        content=b"not json",
        headers={**pw.headers, "content-type": "application/json"},
    )
    assert r.status_code == 400
    assert await rows(select(PartnerCatalog)) == []


async def test_a_test_snapshot_locks_the_vitrine_in_qa(client, world):
    pw = await make_partner(client, world.admin)
    await put_catalog(client, pw, catalog_body(test=True))
    shop_id = (
        await ok(
            client,
            world.admin,
            "createVitrine",
            {"shop": shop_body("Coffee and Tea"), "source": "ods", "partner_id": str(pw.partner.id)},
        )
    )["vitrine"]["shop_id"]
    await switch(client, world.admin, True)
    r = await fn(client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"})
    assert r.status_code == 409 and r.json()["error"] == "catalog_test"
    # a real snapshot: publishable; a test one later puts it back to qa
    await put_catalog(client, pw, catalog_body(version="real"))
    await ok(client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"})
    await put_catalog(client, pw, catalog_body(test=True, version="test-again"))
    assert (await reload(Shop, shop_id)).visibility == "qa"
    assert (await reload(Vitrine, shop_id)).catalog_test is True


async def test_switching_the_catalog_source(client, world):
    pw = await make_partner(client, world.admin)
    await put_catalog(client, pw, catalog_body())
    shop_id, _ = await manual_vitrine(client, world.admin)
    preview = await ok(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "ods", "partner_id": str(pw.partner.id), "dry_run": True},
    )
    assert preview["preview"] == {"incoming_products": 3, "current_products": 2}
    linked = await ok(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "ods", "partner_id": str(pw.partner.id)},
    )
    assert linked["vitrine"]["source"] == "ods" and linked["vitrine"]["counts"]["products"] == 3
    # one restaurant = one Vitrine
    other_id, _ = await manual_vitrine(client, world.admin, name="Autre")
    r = await fn(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": other_id, "source": "ods", "partner_id": str(pw.partner.id)},
    )
    assert r.status_code == 409 and r.json()["error"] == "partner_already_linked"
    # back to the hand-made catalog within 30 days
    restored = await ok(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "manual", "mode": "restore"},
    )
    names = sorted(
        p["name"]
        for p in (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["products"]
    )
    assert restored["vitrine"]["source"] == "manual" and names == ["Café crème", "Croissant"]
    # keep: the ODS products become hand-made ones (no ODS id any more)
    await ok(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "ods", "partner_id": str(pw.partner.id)},
    )
    await ok(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "manual", "mode": "keep"},
    )
    kept = (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["products"]
    assert len(kept) == 3 and all(p["ods_menu_id"] is None for p in kept)
    r = await fn(
        client,
        world.admin,
        "setVitrineCatalogSource",
        {"shop_id": shop_id, "source": "manual", "mode": "restore"},
    )
    assert r.status_code == 409 and r.json()["error"] == "restore_unavailable"


async def test_request_sync_sends_a_signed_webhook(client, world):
    server = WebhookServer().install()
    pw = await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)
    shop_id = (
        await ok(
            client,
            world.admin,
            "createVitrine",
            {"shop": shop_body("Coffee and Tea"), "source": "ods", "partner_id": str(pw.partner.id)},
        )
    )["vitrine"]["shop_id"]
    sent = await ok(client, world.admin, "requestVitrineCatalogSync", {"shop_id": shop_id})
    assert sent["delivered"] is True
    [call] = server.calls
    assert call.body["event"] == "catalog.sync_requested" and call.body["order_id"] is None
    assert call.body["data"]["reason"] == "admin_refresh" and server.verify(call, pw.webhook_secret)
    manual_id, _ = await manual_vitrine(client, world.admin, name="Manuel")
    r = await fn(client, world.admin, "requestVitrineCatalogSync", {"shop_id": manual_id})
    assert r.status_code == 409 and r.json()["error"] == "not_linked"


# --- visits, posters, merchant link ----------------------------------------------------------------------


async def test_visits_and_scans_are_counted_once_per_device_and_day(client, world, qa):
    shop_id, _ = await manual_vitrine(client, world.admin)
    poster = (
        await ok(client, world.admin, "createVitrinePoster", {"shop_id": shop_id, "label": "Comptoir"})
    )["poster"]
    assert poster["code"] == "comptoir" and poster["url"].endswith("/v/chez-amel-sousse?src=comptoir")
    for _ in range(3):
        await ok(client, qa, "getVitrine", {"shop_id": shop_id, "src": "comptoir"})
    await ok(client, qa, "getVitrine", {"shop_id": shop_id, "src": "inconnu"})
    await ok(client, world.admin, "getVitrine", {"shop_id": shop_id})  # admins are not counted
    visits = await rows(select(VitrineVisit))
    assert sorted(v.kind for v in visits) == ["scan:comptoir", "view"]
    v = (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["vitrine"]
    assert v["posters"][0]["scans_month"] == 1
    async with SessionLocal() as s:
        await s.execute(text("UPDATE vitrine_visits SET day = day - 41"))
        await s.commit()
    assert await JOBS["purge_vitrine_visits"].func() == {"deleted": 2}


async def test_merchant_link(client, world, factory, qa):
    shop_id, [cafe, croissant] = await manual_vitrine(client, world.admin)
    await switch(client, world.admin, True)
    await ok(client, world.admin, "setVitrineVisibility", {"shop_id": shop_id, "visibility": "public"})
    link = await ok(client, world.admin, "rotateVitrineMerchantLink", {"shop_id": shop_id})
    token = link["url"].rsplit("/m/", 1)[1]
    assert link["merchant_link"] == {**link["merchant_link"], "number": 1, "opens": 0}
    assert (await ok(client, world.admin, "getVitrineMerchantLink", {"shop_id": shop_id}))["url"] == link[
        "url"
    ]
    v = (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["vitrine"]
    assert token not in json.dumps(v)  # the admin object never holds the token

    # a real order (delivered) and a QA one: only the real one counts on a public Vitrine
    r = await fn(client, world.customer, "placeOrder", cart_order(shop_id, [{"product_id": cafe, "qty": 2}]))
    assert r.status_code == 200, r.text
    real_id = r.json()["order"]["id"]
    r = await fn(client, qa, "placeOrder", cart_order(shop_id, [{"product_id": croissant, "qty": 1}]))
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE orders SET items_text = 'QA TEST ' || items_text WHERE id = :q;"),
            {"q": r.json()["order"]["id"]},
        )
        await s.execute(
            text(
                "UPDATE orders SET status = 'delivered', delivered_at = now(), courier_id = :c WHERE id = :id"
            ),
            {"id": real_id, "c": world.courier.id},
        )
        await s.commit()
    await ok(client, world.customer, "getVitrine", {"shop_id": shop_id})

    page = await client.get(f"/api/public/vitrine/merchant/{token}")
    assert page.status_code == 200, page.text
    assert page.headers["x-robots-tag"].startswith("noindex")
    body = page.json()
    assert body["test_mode"] is False and body["orders_placed"] == 1 and body["orders_delivered"] == 1
    assert body["items_amount"] == 5.0 and body["views"] == 1
    assert body["top_products"] == [{"name": "Café crème", "qty": 2}]
    assert sum(h["orders"] for h in body["hours"]) == 1 and len(body["months"]) == 3
    raw = json.dumps(body)
    customer = await reload(type(world.customer), world.customer.id)
    for secret in (customer.full_name, "Ben Ali", "22111", "+216", "Rue de la plage", customer.email):
        assert secret not in raw
    assert set(body) == {
        "shop", "test_mode", "month", "months", "orders_placed", "orders_delivered", "items_amount", "views",
        "scans", "top_products", "hours",
    }  # fmt: skip
    assert (await client.get(f"/api/public/vitrine/merchant/{token}?month=2020-01")).status_code == 400
    assert (await client.get("/api/public/vitrine/merchant/" + "x" * 43)).status_code == 404
    assert (await reload(Vitrine, shop_id)).merchant_link_opens == 1

    # « Nouveau lien »: the old one stops at once
    new = await ok(client, world.admin, "rotateVitrineMerchantLink", {"shop_id": shop_id})
    assert new["merchant_link"]["number"] == 2
    assert (await client.get(f"/api/public/vitrine/merchant/{token}")).status_code == 404
    new_token = new["url"].rsplit("/m/", 1)[1]
    assert (await client.get(f"/api/public/vitrine/merchant/{new_token}")).status_code == 200


async def test_merchant_link_in_test_mode_counts_qa_orders(client, world, qa):
    shop_id, [cafe, _] = await manual_vitrine(client, world.admin)
    token = (await ok(client, world.admin, "rotateVitrineMerchantLink", {"shop_id": shop_id}))["url"].rsplit(
        "/", 1
    )[1]
    r = await fn(client, qa, "placeOrder", cart_order(shop_id, [{"product_id": cafe, "qty": 1}]))
    assert r.status_code == 200, r.text
    order = await reload(Order, r.json()["order"]["id"])
    assert order.items_text.startswith("QA TEST [Vitrine] Chez Amel")
    body = (await client.get(f"/api/public/vitrine/merchant/{token}")).json()
    assert body["test_mode"] is True and body["orders_placed"] == 1


async def test_merchant_link_rate_limit(client, world):
    shop_id, _ = await manual_vitrine(client, world.admin)
    token = (await ok(client, world.admin, "rotateVitrineMerchantLink", {"shop_id": shop_id}))["url"].rsplit(
        "/", 1
    )[1]
    codes = [(await client.get(f"/api/public/vitrine/merchant/{token}")).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_stale_after_26_hours(client, world):
    pw = await make_partner(client, world.admin)
    await put_catalog(client, pw, catalog_body())
    shop_id = (
        await ok(
            client,
            world.admin,
            "createVitrine",
            {"shop": shop_body("Coffee and Tea"), "source": "ods", "partner_id": str(pw.partner.id)},
        )
    )["vitrine"]["shop_id"]
    async with SessionLocal() as s:
        old = datetime.now(UTC) - timedelta(hours=27)
        await s.execute(text("UPDATE vitrines SET catalog_synced_at = :t"), {"t": old})
        await s.execute(text("UPDATE partner_catalogs SET checked_at = :t"), {"t": old})
        await s.commit()
    v = (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["vitrine"]
    assert v["catalog"]["stale"] is True
    # an unchanged push refreshes « ODS est joignable » (checked_at), still without rewriting the catalog
    assert (await put_catalog(client, pw, catalog_body())).json()["status"] == "unchanged"
    v = (await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id}))["vitrine"]
    assert v["catalog"]["stale"] is False
    assert vitrine_stats.recent_months()[0] == datetime.now(UTC).astimezone(vitrine_service.TUNIS).strftime(
        "%Y-%m"
    )
