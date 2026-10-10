"""searchPlaces / searchByBbox on real PostGIS rows: radius, bounding box, text scores
(accents, Arabic), category vocabulary, visibility of shop proposals, pagination."""

import math

import pytest

from app.compat import jsnum
from app.services import text_norm
from app.services.places import (
    Bbox,
    bbox_category_matches,
    haversine_m,
    js_round,
    same_category,
    validate_bbox,
)
from tests.catalog_data import LA_MARSA, SOUSSE, TUNIS, add_place, add_shop
from tests.factories import auth, error_of

LAT, LNG = TUNIS


def north(km: float) -> tuple[float, float]:
    """A point `km` north of Tunis centre."""
    return LAT + km / 111.195, LNG


# --- normalization and JS coercions ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tokens"),
    [
        ("Café de l'Hôpital", ["cafe", "de", "l", "hopital"]),
        ("  SUPERMARCHÉ   Aziza!! ", ["supermarche", "aziza"]),
        ("Rue n°5, Ariana", ["rue", "n", "5", "ariana"]),
        ("مَطْعَم السّلام", ["مطعم", "السلام"]),  # harakat and shadda removed
        ("صيدليـــة النور", ["صيدلية", "النور"]),  # tatweel removed
        ("", []),
        (None, []),
    ],
)
def test_tokenize(raw, tokens):
    assert text_norm.tokenize(raw) == tokens


def test_normalization_helpers():
    assert text_norm.normalize_text("Pâtisserie", "  Tunis ") == "patisserie tunis"
    assert text_norm.compact("Chez  Ali - Café") == "chezalicafe"
    assert text_norm.fold_trim("  Hôpital Charles-Nicolle ") == "hopital charles-nicolle"
    assert text_norm.search_text("Pharmacie", None, "La Marsa") == "pharmacie la marsa"


def test_js_number_coercions():
    assert jsnum.js_number(None) == 0 and jsnum.js_number("") == 0 and jsnum.js_number(True) == 1
    assert jsnum.js_number(" 36.8 ") == 36.8 and math.isnan(jsnum.js_number("abc"))
    assert jsnum.js_number("Infinity") == math.inf and jsnum.js_number("-Infinity") == -math.inf
    assert math.isnan(jsnum.js_number("inf")) and math.isnan(jsnum.js_number([1]))
    assert jsnum.number_or("0", 8) == 8 and jsnum.number_or("5", 8) == 5 and jsnum.number_or("x", 8) == 8
    assert (
        jsnum.parse_int("12px") == 12
        and math.isnan(jsnum.parse_int("px"))
        and math.isnan(jsnum.parse_int(None))
    )
    assert jsnum.parse_float("4.5 stars") == 4.5 and math.isnan(jsnum.parse_float("x"))
    assert (
        jsnum.is_finite_number(1.5) and not jsnum.is_finite_number("1.5") and not jsnum.is_finite_number(True)
    )
    assert not jsnum.is_finite_number(math.inf)
    assert math.isnan(jsnum.clamp(math.nan, 1, 2)) and jsnum.clamp(500, 1, 100) == 100
    assert js_round(2.345, 2) == 2.35 and js_round(0.5) == 1


def test_category_rules():
    assert same_category("supermarché", "supermarche") and same_category("", "banque")
    assert same_category("pharmacie", None) and not same_category("banque", "pharmacie")
    assert bbox_category_matches("pharmacy", "pharmacie") and bbox_category_matches("fast_food", "restaurant")
    assert bbox_category_matches("hôpital", "hôpital") and not bbox_category_matches("banque", "carburant")
    assert bbox_category_matches("Boulangerie Fraîche", "Boulangerie Fraîche")


# --- searchPlaces --------------------------------------------------------------------------


async def test_search_places_guards(client, factory):
    assert (
        await client.post("/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG})
    ).status_code == 401
    user = await factory.user()
    for body in ({}, {"lat": "36.8", "lng": 10.1}, {"lat": LAT}, {"lat": LAT, "lng": None}):
        r = await client.post("/api/functions/searchPlaces", json=body, headers=auth(user))
        assert r.status_code == 400 and r.json() == {"error": "Location required"}


async def test_search_places_radius_ranking_and_shape(client, factory):
    user = await factory.user()
    near = await add_place("Pizzeria Roma", *north(0.5), quality=80, city="Tunis")
    await add_place("Café de l'Hôpital", *north(1.0), address="Rue de l'Hôpital")
    await add_place("Pharmacie Centrale", *north(2.0), category="pharmacie")
    await add_place("Loin", *LA_MARSA)  # ~16 km: outside 8 km
    r = await client.post("/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG}, headers=auth(user))
    assert r.status_code == 200
    body = r.json()
    names = [p["name"] for p in body["places"]]
    assert names == ["Pizzeria Roma", "Café de l'Hôpital", "Pharmacie Centrale"]
    assert body["success"] is True and body["total"] == 3
    first = body["places"][0]
    assert first["id"] == str(near.id) and first["osm_id"] == near.osm_id and first["created_by"] is None
    assert (
        first["quality_score"] == 0.8 and first["city"] == "Tunis" and first["name_norm"] == "pizzeria roma"
    )
    assert abs(first["lat"] - north(0.5)[0]) < 1e-9 and first["lng"] == pytest.approx(LNG)
    assert first["distance_km"] == pytest.approx(0.5, abs=0.01)
    # 0.45 * 0.5 (no text) + 0.25 * (1 - 0.5 / 8) + 0.2 + 0.1 * 0.8
    assert first["score"] == pytest.approx(0.225 + 0.25 * (1 - 0.5 / 8) + 0.2 + 0.08, abs=0.0015)
    assert first["created_date"].count("T") == 1 and first["created_date"].endswith("Z")  # B18: zoned


async def test_search_places_text_scores_accents_and_arabic(client, factory):
    user = await factory.user()
    await add_place("Pizzeria Roma", *north(0.3))
    await add_place("Café de l'Hôpital", *north(3.0), address="Avenue Habib Bourguiba")
    await add_place("مَطْعَم السلام", *north(4.0))
    await add_place("Boulangerie El Hana", *north(5.0), category="boulangerie")

    async def top(query: str) -> str:
        r = await client.post(
            "/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG, "query": query}, headers=auth(user)
        )
        return r.json()["places"][0]["name"]

    assert await top("hopital") == "Café de l'Hôpital"
    assert await top("HÔPITAL bourguiba") == "Café de l'Hôpital"
    assert await top("مطعم") == "مَطْعَم السلام"
    assert await top("مَطْعَم") == "مَطْعَم السلام"
    # the category is part of the text haystack
    assert await top("boulangerie") == "Boulangerie El Hana"
    # partial token (substring of a word), as JS `includes`
    assert await top("pizz") == "Pizzeria Roma"


async def test_search_places_filters_bounds_and_dedupe(client, factory):
    user = await factory.user()
    await add_place("Aziza", *north(1), category="supermarché", governorate="Tunis")
    await add_place("Aziza", *north(1), category="supermarché")  # same name + position: deduplicated
    await add_place("Monoprix", *north(2), category="supermarché", governorate="Sousse")  # known, different
    await add_place(
        "MG", *north(2.5), category="supermarket"
    )  # not the same word: sameCategory is a substring
    await add_place("Carrefour Market", *north(3), category="supermarché", city="La Marsa")
    await add_place("Pharmacie", *north(1.5), category="pharmacie")

    async def names(**extra) -> list[str]:
        r = await client.post(
            "/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG, **extra}, headers=auth(user)
        )
        assert r.status_code == 200
        return [p["name"] for p in r.json()["places"]]

    assert await names(category="supermarche") == ["Aziza", "Monoprix", "Carrefour Market"]
    assert await names(category="supermarché", governorate="tunis") == ["Aziza", "Carrefour Market"]
    assert await names(category="supermarché", city="LA MARSA") == ["Aziza", "Monoprix", "Carrefour Market"]
    assert await names(governorate="Tunis", city="Ariana") == ["Aziza", "Pharmacie", "MG"]
    assert await names(limit=2) == ["Aziza", "Pharmacie"]
    assert await names(limit="0") == ["Aziza", "Pharmacie", "Monoprix", "MG", "Carrefour Market"]  # 0 || 20
    assert await names(radius_km=0.05) == []  # clamped to 0.1 km: nothing that close
    assert await names(radius_km=1.2) == ["Aziza"]
    assert len(await names(radius_km=100000)) == 5  # clamped to 100 km


async def test_search_places_dedupe_pages_through_duplicates(client, factory):
    user = await factory.user()
    for _ in range(4):
        await add_place("Twin", *north(0.2), quality=90)
    await add_place("Other", *north(0.4), quality=10)
    r = await client.post(
        "/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG, "limit": 2}, headers=auth(user)
    )
    assert [p["name"] for p in r.json()["places"]] == ["Twin", "Other"]


# --- searchByBbox --------------------------------------------------------------------------

BOX = {"minLat": LAT - 0.05, "maxLat": LAT + 0.05, "minLng": LNG - 0.05, "maxLng": LNG + 0.05}


def test_validate_bbox_messages():
    ok = Bbox(36.7, 36.9, 10.1, 10.3)
    assert validate_bbox(ok) is None
    assert validate_bbox(Bbox(math.nan, 36.9, 10.1, 10.3)) == "Invalid latitude values"
    assert validate_bbox(Bbox(36.7, 36.9, math.inf, 10.3)) == "Invalid longitude values"
    assert validate_bbox(Bbox(36.9, 36.7, 10.1, 10.3)) == "minLat must be less than maxLat"
    assert validate_bbox(Bbox(36.7, 36.9, 10.3, 10.1)) == "minLng must be less than maxLng"
    assert validate_bbox(Bbox(48.0, 49.0, 2.0, 3.0)) == "Bounding box outside Tunisia"
    assert validate_bbox(Bbox(36.7, 36.9, 13.0, 14.0)) == "Bounding box outside Tunisia"


async def test_search_by_bbox_guards(client, factory):
    assert (await client.post("/api/functions/searchByBbox", json=BOX)).status_code == 401
    user = await factory.user()
    missing = await client.post("/api/functions/searchByBbox", json={}, headers=auth(user))
    assert missing.status_code == 400 and error_of(missing) == "minLat must be less than maxLat"
    bad = await client.post(
        "/api/functions/searchByBbox", json={**BOX, "minLat": "north"}, headers=auth(user)
    )
    assert bad.status_code == 400 and error_of(bad) == "Invalid latitude values"


async def test_search_by_bbox_contents_order_and_visibility(client, factory):
    viewer, other, admin = await factory.user(), await factory.user(), await factory.user(role="admin")
    best = await add_place("Top Resto", LAT + 0.04, LNG + 0.04, quality=95, phone="+21671000000")
    await add_place("Near Resto", LAT + 0.001, LNG, quality=60)
    await add_place("Far Resto", LAT + 0.02, LNG, quality=60)
    await add_place("Outside", LAT + 0.051, LNG, quality=99)  # just north of the box
    approved = await add_shop("Chez Ali", LAT, LNG + 0.01, categories=["restaurant"], address="Rue X")
    mine = await add_shop("Ma proposition", LAT, LNG + 0.02, review_status="pending", proposed_by=viewer)
    await add_shop("Leur proposition", LAT, LNG + 0.02, review_status="pending", proposed_by=other)
    await add_shop("Refusé", LAT, LNG + 0.03, review_status="rejected", proposed_by=viewer)

    r = await client.post("/api/functions/searchByBbox", json=BOX, headers=auth(viewer))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["bbox"] == BOX and data["total_count"] == 5 and data["cursor_next"] is None
    ids = [row["id"] for row in data["results"]]
    assert ids[0] == f"place_index:{best.id}"
    assert [row["name"] for row in data["results"]] == [
        "Top Resto", "Near Resto", "Far Resto", "Chez Ali", "Ma proposition",
    ]  # fmt: skip
    top = data["results"][0]
    assert "shop_id" not in top and top["source"] == "place_index" and top["rating"] == 0.95
    assert top["phone"] == "+21671000000" and top["address"] == "" and top["review_count"] == 0
    assert top["distance_meters"] == round(haversine_m(LAT, LNG, LAT + 0.04, LNG + 0.04))
    shop = data["results"][3]
    assert shop == {
        "id": f"shop:{approved.id}", "shop_id": str(approved.id), "source": "shop", "name": "Chez Ali",
        "category": "restaurant", "lat": pytest.approx(LAT), "lng": pytest.approx(LNG + 0.01), "rating": 0,
        "review_count": 0, "distance_meters": shop["distance_meters"], "phone": "", "address": "Rue X",
        "city": "",
    }  # fmt: skip
    assert data["results"][4]["shop_id"] == str(mine.id)

    other_view = (await client.post("/api/functions/searchByBbox", json=BOX, headers=auth(other))).json()
    assert "Leur proposition" in [row["name"] for row in other_view["data"]["results"]]
    assert "Ma proposition" not in [row["name"] for row in other_view["data"]["results"]]
    admin_view = (await client.post("/api/functions/searchByBbox", json=BOX, headers=auth(admin))).json()
    assert admin_view["data"]["total_count"] == 4  # approved shop + 3 places: proposals are per author


async def test_search_by_bbox_filters_and_pagination(client, factory):
    user = await factory.user()
    await add_place("Pharmacie Ibn Sina", LAT + 0.01, LNG, category="pharmacie", quality=90)
    await add_place("Pharmacie de Nuit", LAT + 0.02, LNG, category="pharmacy", quality=80)
    await add_place("Station Agil", LAT + 0.03, LNG, category="carburant", quality=70)
    await add_place("Boulangerie", LAT + 0.01, LNG + 0.01, category="boulangerie", quality=None)
    await add_shop("Pharma Shop", LAT, LNG, categories=["pharmacy"], city="Tunis")

    async def query(params=None, **body):
        r = await client.post(
            "/api/functions/searchByBbox", params=params, json={**BOX, **body}, headers=auth(user)
        )
        assert r.status_code == 200, r.text
        return r.json()["data"]

    pharmacies = await query(category="pharmacie")
    assert [row["name"] for row in pharmacies["results"]] == [
        "Pharmacie Ibn Sina", "Pharmacie de Nuit", "Pharma Shop",
    ]  # fmt: skip
    assert [r["name"] for r in (await query(search_query="pharmacie nuit"))["results"]] == [
        "Pharmacie de Nuit"
    ]
    assert [r["name"] for r in (await query(search_query="tunis"))["results"]] == ["Pharma Shop"]
    # the category is searchable text too
    assert [r["name"] for r in (await query(search_query="carburant"))["results"]] == ["Station Agil"]
    assert [r["name"] for r in (await query(min_rating=0.85))["results"]] == ["Pharmacie Ibn Sina"]

    first = await query(limit=2)
    assert first["total_count"] == 5 and len(first["results"]) == 2 and first["cursor_next"] == "2"
    second = await query(limit=2, cursor=first["cursor_next"])
    assert [r["name"] for r in second["results"]] == ["Station Agil", "Pharma Shop"]
    assert second["cursor_next"] == "4"
    last = await query(limit=2, cursor="4")
    assert [r["name"] for r in last["results"]] == ["Boulangerie"] and last["cursor_next"] is None
    assert last["results"][0]["rating"] == 0

    # query string first (GET-style), body ignored for the same key; bad limit → default
    by_query = await query(params={"category": "carburant", "limit": "abc"}, category="pharmacie")
    assert [r["name"] for r in by_query["results"]] == ["Station Agil"]
    assert len((await query(limit=500))["results"]) == 5


async def test_search_by_bbox_large_box_uses_whole_area(client, factory):
    user = await factory.user()
    await add_place("Sousse Resto", *SOUSSE)
    await add_place("Tunis Resto", *TUNIS)
    whole = {"minLat": 30.2, "maxLat": 37.6, "minLng": 7.5, "maxLng": 11.6}
    r = await client.post("/api/functions/searchByBbox", json=whole, headers=auth(user))
    assert r.json()["data"]["total_count"] == 2


# --- QA 06/10, B1: a typed name is searched in every category, and only matches are listed --


async def test_search_places_typed_name_ignores_category_and_drops_unrelated(client, factory):
    user = await factory.user()
    await add_place("Café Victor Hugo", *north(0.2), category="restaurant")
    await add_place("Monoprix Lafayette", *north(1.0), category="supermarché")
    await add_place("Monoprix Menzah", *north(2.0), category="supermarket")
    await add_place("Dentiste Dr Ali", *north(0.3), category="dentist")
    await add_place("BTE Banque", *north(0.4), category="banque")

    async def names(**extra) -> list[str]:
        r = await client.post(
            "/api/functions/searchPlaces", json={"lat": LAT, "lng": LNG, **extra}, headers=auth(user)
        )
        assert r.status_code == 200, r.text
        return [p["name"] for p in r.json()["places"]]

    # « Top cafés » selected (the shop picker sends its category): the Monoprix are still found
    assert await names(query="Monoprix", category="restaurant") == ["Monoprix Lafayette", "Monoprix Menzah"]
    # Explorer (no category): only the Monoprix, no dentist, bank or café after them
    assert await names(query="monoprix", limit=15) == ["Monoprix Lafayette", "Monoprix Menzah"]
    # two words: places with both words only, when some exist
    assert await names(query="monoprix menzah") == ["Monoprix Menzah"]
    # a typo in one word still finds the place by the other word
    assert await names(query="monoprix menzhaa") == ["Monoprix Lafayette", "Monoprix Menzah"]
    # the chosen category still lifts its places when several match
    assert (await names(query="monoprix", category="supermarket"))[0] == "Monoprix Menzah"
    # no text: the category is still a filter (browsing « Top supermarchés »)
    assert await names(category="supermarche") == ["Monoprix Lafayette"]
    assert await names(query="nothing-like-this") == []


async def test_search_by_bbox_typed_name_ignores_category(client, factory):
    user = await factory.user()
    await add_place("Monoprix", LAT + 0.01, LNG, category="supermarché", quality=80)
    await add_place("Café Sport", LAT + 0.02, LNG, category="restaurant", quality=80)
    await add_shop("Monoprix Express", LAT, LNG + 0.01, categories=["supermarket"])

    r = await client.post(
        "/api/functions/searchByBbox",
        json={**BOX, "category": "restaurant", "search_query": "monoprix"},
        headers=auth(user),
    )
    assert r.status_code == 200, r.text
    assert sorted(row["name"] for row in r.json()["data"]["results"]) == ["Monoprix", "Monoprix Express"]
    browse = await client.post(
        "/api/functions/searchByBbox", json={**BOX, "category": "restaurant"}, headers=auth(user)
    )
    assert [row["name"] for row in browse.json()["data"]["results"]] == ["Café Sport"]
