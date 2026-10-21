"""Recettes (21/10/2026, first version): quantities (pure), the honest cost, the import, the visibility (test
accounts only), the client reads, the ODS price cache, the recipe order (a normal Achats order) and the
admin. One short test per function."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db import SessionLocal, transaction
from app.models import AuditLog, Order, RecipeOrder, RecipePriceCache
from app.services import partner_webhooks, recipe_prices, recipes_import
from app.services import recipe_units as ru
from tests.order_helpers import SOUSSE_HOME
from tests.vitrine_helpers import QA_CUSTOMER, fn, ok

TODAY = date(2026, 10, 21)


def ref(iid, key, fr, ar, unit, aisle, pantry=False, piece_kg=None):
    """One referential CSV row (strings, as csv.DictReader gives them)."""
    row = {"id": iid, "key": key, "fr": fr, "ar": ar, "unit": unit, "aisle": aisle, "pantry": str(pantry)}
    return {**row, "piece_kg": str(piece_kg)} if piece_kg else row


INGREDIENTS = [
    ref(1, "tomate", "Tomate", "طماطم", "kg", "marche"),
    ref(3, "oignon", "Oignon", "بصل", "kg", "marche"),
    ref(17, "persil", "Persil", "معدنوس", "botte", "marche", piece_kg=0.1),
    ref(41, "merguez", "Merguez", "مرقاز", "kg", "boucherie"),
    ref(77, "oeufs", "Oeufs", "بيض", "piece", "cremerie", piece_kg=0.06),
    ref(116, "huile d'olive", "Huile d'olive", "زيت زيتون", "l", "epicerie", pantry=True),
    ref(118, "harissa", "Harissa", "هريسة", "kg", "epicerie", pantry=True),
]
OJJA = {
    "slug": "ojja-merguez",
    "title_fr": "Ojja merguez",
    "title_ar": "عجة بالمرقاز",
    "category": "plat",
    "time_min": 25,
    "servings": 4,
    "steps_fr": ["Faire revenir la merguez.", "Ajouter les œufs."],
    "steps_ar": ["حمّر المرقاز.", "أضف البيض."],
    "ingredients": [
        {"ingredient_id": 41, "qty": 0.5, "unit": "kg"},
        {"ingredient_id": 1, "qty": 0.6, "unit": "kg"},
        {"ingredient_id": 77, "qty": 6, "unit": "piece"},
        {"ingredient_id": 17, "qty": 1, "unit": "botte"},
        {"ingredient_id": 116, "qty": 2, "unit": "cas"},
        {"ingredient_id": 118, "qty": 1, "unit": "cas"},
    ],
}
SALADE = {
    "slug": "salade-tomate",
    "title_fr": "Salade de tomates",
    "title_ar": "سلطة طماطم",
    "category": "salade",
    "time_min": 10,
    "servings": 2,
    "ingredients": [
        {"ingredient_id": 1, "qty": 0.4, "unit": "kg"},
        {"ingredient_id": 3, "qty": 0.1, "unit": "kg"},
        {"ingredient_id": 116, "qty": 1, "unit": "cas"},
    ],
}


def price(lo, hi, unit="kg", **kw):
    return {"min": lo, "max": hi, "unit": unit, "method": "retail", "source": "Carrefour Tunisie (en ligne)",
            "as_of": "2026-10-21", "stale": False, "to_check": False, **kw}  # fmt: skip


PRICES = {
    1: price(1.85, 2.3),
    3: price(1.725, 1.725),
    17: None,
    41: price(45.2, 46.8),
    77: price(0.44, 0.48, "piece"),
}
ODS_PRICES = {
    "country": "TN",
    "rules": {"min_priced_share": 0.7, "stale_days": 14, "rounding_tnd": 0.5},
    "ingredients": [
        {"id": r["id"], "key": r["key"], "name_fr": r["fr"], "name_ar": r["ar"], "aisle": r["aisle"],
         "buy_unit": r["unit"], "pantry": False, "price": PRICES[r["id"]]}
        for r in INGREDIENTS
        if r["id"] in PRICES
    ],
}  # fmt: skip


def ing(row: dict) -> ru.Ing:
    r = recipes_import.ingredient_row(row)
    return ru.Ing(**{k: r[k] for k in ("id", "key", "unit", "aisle", "pantry", "piece_kg", "density")},
                  name_fr=r["name_fr"], name_ar=r["name_ar"])  # fmt: skip


ING = {row["id"]: ing(row) for row in INGREDIENTS}


async def seed(*recipes, publish=True):
    async with transaction() as session:
        await recipes_import.import_ingredients(session, INGREDIENTS)
        report = await recipes_import.import_recipes(session, list(recipes))
        if publish:
            await session.execute(text("UPDATE recipes SET status='published', published_at=now()"))
    return report


async def recipe_id(slug):
    async with SessionLocal() as s:
        return str((await s.execute(text("SELECT id FROM recipes WHERE slug=:s"), {"s": slug})).scalar_one())


@pytest.fixture
def ods_prices(monkeypatch):
    """ODS answers the signed GET with ODS_PRICES; `calls` records each request."""
    calls: list[httpx.Request] = []
    state = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"detail": "down"})
        return httpx.Response(200, json=ODS_PRICES)

    monkeypatch.setattr(settings, "ODS_API_BASE_URL", "http://ods.test")
    monkeypatch.setattr(settings, "ODS_RECIPES_SECRET", "s" * 40)
    recipe_prices.set_transport(httpx.MockTransport(handler))
    yield calls, state
    recipe_prices.set_transport(None)


@pytest.fixture
async def qa(factory):
    """An ADMIN using a test account (owner 21/10: the Recettes are admin only; only such an admin orders)."""
    return await factory.user(
        email=QA_CUSTOMER, full_name="Lemel Qa", phone_e164="+21622111333", role="admin"
    )


# --- pure ---------------------------------------------------------------------------------------------


def test_units_scale_round_merge_and_words():
    assert ru.scale(Decimal("0.5"), 6, 4) == Decimal("0.75")
    assert ru.round_buy(Decimal("0.03"), "kg") == Decimal("0.1")
    assert ru.round_buy(Decimal("0.33"), "kg") == Decimal("0.35")
    assert ru.round_buy(Decimal("1.23"), "kg") == Decimal("1.3")
    assert ru.round_buy(Decimal("0.3"), "l") == Decimal("0.5")
    assert ru.round_buy(Decimal("1.2"), "botte") == 2
    assert ru.to_buy_unit(2, "cas", ING[116]) == Decimal("0.030")
    assert ru.to_buy_unit(1, "kg", ING[1]) == 1
    assert ru.to_buy_unit(2, "piece", ING[1]) is None  # a piece of something sold by kg: never guessed
    merged = ru.merge([(ING[1], Decimal("0.6")), (ING[3], Decimal("0.1")), (ING[1], Decimal("0.4"))])
    assert [(i.id, q) for i, q in merged] == [(1, Decimal("1.0")), (3, Decimal("0.1"))]
    assert ru.fmt_qty(Decimal("0.35"), "kg", "fr") == "0,35\u00a0kg"
    assert ru.fmt_qty(Decimal("1.5"), "l", "ar") == "1.5\u00a0ل"
    assert ru.fmt_qty(4, "piece", "fr") == "4\u00a0pièces"
    assert ru.fmt_qty(1, "botte", "ar") == "1\u00a0حزمة"


def test_cost_is_a_range_names_the_unpriced_and_hides_the_total_under_70_percent():
    full = recipe_prices.cost(
        [(ING[41], Decimal("0.5")), (ING[1], Decimal("0.6")), (ING[77], 6)], ODS_PRICES, TODAY
    )
    assert full["show_total"] and (full["low"], full["high"]) == (26.0, 28.0)
    assert full["budget_max"] == 33  # known high 28 + 15 % = 32,2, rounded up to the dinar
    assert full["sources"] == ["Carrefour Tunisie (en ligne)"] and not full["stale"]
    with_persil = recipe_prices.cost([(ING[1], Decimal("0.6")), (ING[17], 1)], ODS_PRICES, TODAY)
    assert with_persil["unpriced"] == [{"id": 17, "fr": "Persil", "ar": "معدنوس"}]
    assert with_persil["show_total"] is False and with_persil["low"] is None  # 1 line of 2 priced
    assert with_persil["known_high"] == 1.5
    assert recipe_prices.cost([(ING[1], 1)], None) == {"available": False}  # no price list: no cost at all
    old = json.loads(json.dumps(ODS_PRICES))
    old["ingredients"][0]["price"]["as_of"] = "2026-09-30"
    assert recipe_prices.cost([(ING[1], 1)], old, TODAY)["stale"] is True


# --- import -------------------------------------------------------------------------------------------


def test_a_pastry_counts_pieces_and_a_spice_mix_portions():
    unit = recipes_import._servings_unit
    assert (
        unit({"portions_unite": "pièces"}) == "pieces" and unit({"portions_unite": "portions"}) == "portions"
    )
    assert unit({}) == "people" and unit({"portions_unite": "verres"}) == "people"


async def test_import_is_idempotent_keeps_unknown_lines_and_sends_a_changed_recipe_back_to_draft():
    odd = {
        "slug": "x",
        "title_fr": "X",
        "ingredients": [{"ingredient_id": 999, "nom_fr": "Truc", "qty": 1, "unit": "kg"}],
    }
    first = await seed(OJJA, SALADE, odd)
    assert (first["created"], first["refused"]) == (3, [])
    assert first["warnings"] == ["x: « Truc » (999) hors dictionnaire : gardé sans prix"]
    again = await seed(OJJA, SALADE, odd, publish=False)
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 3)
    changed = await seed({**OJJA, "time_min": 30}, publish=False)
    assert changed["back_to_draft"] == ["ojja-merguez"]
    async with SessionLocal() as s:
        rows = dict((await s.execute(text("SELECT slug, status FROM recipes"))).all())
        lines = (await s.execute(text("SELECT count(*) FROM recipe_lines"))).scalar_one()
    assert rows == {"ojja-merguez": "draft", "salade-tomate": "published", "x": "published"} and lines == 10


# --- visibility and client reads ------------------------------------------------------------------------


async def test_nobody_but_an_admin_reads_or_orders_recipes_even_a_test_account_or_the_public_switch_on(
    client, factory, ods_prices
):
    """Owner 21/10: the Recettes are ADMIN ONLY for now — customers, couriers and the test accounts get 403
    on every recipe route, also once « Recettes publiques » is on; an admin reads; only an admin on a test
    account may order (« QA TEST »), any other admin is refused."""
    await seed(OJJA)
    rid = await recipe_id("ojja-merguez")
    qa_customer = await factory.user(email=QA_CUSTOMER, full_name="Lemel Qa", phone_e164="+21622111333")
    someone = await factory.user()
    courier = await factory.user(role="courier")
    admin = await factory.user(role="admin")

    async def closed(user):
        for name, body in (
            ("listRecipes", {}),
            ("getRecipe", {"slug": "ojja-merguez"}),
            ("previewRecipeList", {"items": [{"recipe_id": str(rid), "servings": 4}]}),
            ("placeOrder", order_body(rid, accept_unpriced=True)),
        ):
            r = await fn(client, user, name, body)
            assert r.status_code == 403, (name, r.status_code, r.text)

    for user in (someone, qa_customer, courier):
        await closed(user)
    assert (await fn(client, someone, "setRecipesSettings", {"public_allowed": True})).status_code == 403
    await ok(client, admin, "setRecipesSettings", {"public_allowed": True})
    for user in (someone, qa_customer, courier):
        await closed(user)  # the switch alone never opens them (recipes.ADMIN_ONLY)
    seen = await ok(client, admin, "listRecipes")
    assert seen["visible"] and [r["slug"] for r in seen["recipes"]] == ["ojja-merguez"]
    assert (await ok(client, admin, "getRecipe", {"slug": "ojja-merguez"}))["recipe"][
        "slug"
    ] == "ojja-merguez"
    refused = await fn(client, admin, "placeOrder", order_body(rid, accept_unpriced=True))
    assert refused.status_code == 403 and refused.json()["error"] == "recipes_order_disabled"
    async with SessionLocal() as s:
        assert (await s.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0


async def test_get_recipe_scales_for_the_people_pre_ticks_the_pantry_and_prices_the_rest(
    client, qa, ods_prices
):
    await seed(OJJA)
    r = (await ok(client, qa, "getRecipe", {"slug": "ojja-merguez", "servings": 6}))["recipe"]
    by_id = {i["ingredient_id"]: i for i in r["ingredients"]}
    assert by_id[41]["text_fr"] == "0,75\u00a0kg" and by_id[41]["buy"]["text_fr"] == "0,75\u00a0kg"
    assert by_id[77]["qty"] == 9 and by_id[77]["buy"]["text_ar"] == "9\u00a0حبات"
    assert by_id[116]["at_home"] and by_id[116]["buy"] is None and by_id[116]["text_fr"] == "3\u00a0c. à s."
    assert r["cost"]["show_total"] is True and r["cost"]["unpriced"] == [
        {"id": 17, "fr": "Persil", "ar": "معدنوس"}
    ]
    assert r["steps_ar"] == ["حمّر المرقاز.", "أضف البيض."]
    unticked = (await ok(client, qa, "getRecipe", {"slug": "ojja-merguez", "at_home": []}))["recipe"]
    assert {i["ingredient_id"]: i["buy"]["text_fr"] for i in unticked["ingredients"]}[116] == "0,25\u00a0L"


async def test_preview_merges_two_recipes_by_ingredient_before_rounding(client, qa, ods_prices):
    await seed(OJJA, SALADE)
    items = [{"recipe_id": await recipe_id("ojja-merguez"), "servings": 4},
             {"recipe_id": await recipe_id("salade-tomate"), "servings": 4}]  # fmt: skip
    p = await ok(client, qa, "previewRecipeList", {"items": items})
    tomate = next(line for line in p["lines"] if line["ingredient_id"] == 1)
    assert tomate["qty"] == 1.4 and tomate["price"]["priced"] is True  # 0,6 + 2 × 0,4
    assert [line["aisle"] for line in p["lines"]] == ["marche", "marche", "marche", "boucherie", "cremerie"]
    assert {h["ingredient_id"] for h in p["at_home"]} == {116, 118}
    assert p["weight_kg"] == pytest.approx(1.4 + 0.2 + 0.1 + 0.5 + 0.36)  # a bunch 0,1 kg, an egg 0,06 kg
    bad = await fn(client, qa, "previewRecipeList", {"items": [{"recipe_id": "nope"}]})
    assert bad.status_code == 400 and bad.json()["error"] == "recipes_unavailable"


# --- ODS prices -------------------------------------------------------------------------------------------


async def test_prices_are_read_signed_once_then_cached_and_ods_down_means_no_cost(client, qa, ods_prices):
    calls, state = ods_prices
    await seed(OJJA)
    await ok(client, qa, "listRecipes")
    await ok(client, qa, "listRecipes")
    assert len(calls) == 1
    req = calls[0]
    assert req.url.path == "/api/v1/market/recipe-prices" and req.url.params["country"] == "TN"
    assert partner_webhooks.verify(
        "s" * 40, req.headers["X-ODSD-Timestamp"], b"", req.headers["X-ODSD-Signature"]
    )
    async with transaction() as s:  # the list is old and ODS is down: the old prices are kept
        row = await s.get(RecipePriceCache, "TN")
        row.fetched_at = datetime.now(UTC) - timedelta(days=1)
    state["status"] = 503
    assert (await ok(client, qa, "listRecipes"))["recipes"][0]["cost"]["available"] is True
    async with transaction() as s:
        await s.execute(text("DELETE FROM recipe_price_cache"))
    cards = (await ok(client, qa, "listRecipes"))["recipes"]
    assert cards[0]["cost"] == {"available": False} and cards[0]["per_person"] is None


# --- the order ------------------------------------------------------------------------------------------


def order_body(rid, **recipes):
    return {
        "order": {
            "recipes": {"items": [{"recipe_id": rid, "servings": 4}], "lang": "fr", **recipes},
            "delivery_address": "Rue de la plage 12",
            "delivery_lat": SOUSSE_HOME[0],
            "delivery_lng": SOUSSE_HOME[1],
            "customer_phone": "22111333",
            "client_ref": "rec-1",
        }
    }


async def test_recipe_order_is_a_normal_achats_order_marked_qa_test_with_the_list_rebuilt(
    client, qa, ods_prices
):
    await seed(OJJA)
    async with transaction() as s:
        await s.execute(
            text(
                "INSERT INTO places (osm_id, name, name_norm, category, location) VALUES "
                "('n1', 'Monoprix Sahloul', 'monoprix sahloul', 'supermarché', "
                "ST_SetSRID(ST_MakePoint(10.6190, 35.8310), 4326)::geography)"
            )
        )
    rid = await recipe_id("ojja-merguez")
    refused = await fn(client, qa, "placeOrder", order_body(rid))
    assert refused.status_code == 400 and refused.json() == {
        "error": "recipes_unpriced_not_accepted",
        "count": 1,
    }
    body = order_body(rid, accept_unpriced=True, at_home=[118])  # the olive oil is NOT at home this time
    placed = await ok(client, qa, "placeOrder", body)
    again = await ok(client, qa, "placeOrder", body)  # same attempt key: the same order, never a second
    assert again["order"]["id"] == placed["order"]["id"] and again["existing"] is True
    async with SessionLocal() as s:
        order = await s.get(Order, placed["order"]["id"])
        rec = await s.get(RecipeOrder, order.id)
    assert order.items_text.split("\n") == [
        "QA TEST [Recettes] Ojja merguez (4 pers.)",
        "Persil : 1\u00a0botte",
        "Tomate : 0,6\u00a0kg",
        "Merguez : 0,5\u00a0kg",
        "Oeufs : 6\u00a0pièces",
        "Huile d'olive : 0,25\u00a0L",
    ]
    assert order.source == "app" and order.unavailable_policy == "substitute"
    assert (
        order.budget_max is None
    )  # olive oil and persil without a price: 2 of 5 lines → no total, no ceiling
    assert "Persil" in order.notes and order.package == "petit"
    assert (await reload_stop_name(order.id)) == "Monoprix Sahloul"
    assert rec.test is True and rec.at_home == [118] and rec.recipes[0]["servings"] == 4


async def reload_stop_name(order_id):
    async with SessionLocal() as s:
        return (
            await s.execute(text("SELECT name FROM order_stops WHERE order_id=:o"), {"o": order_id})
        ).scalar_one()


async def test_recipe_order_with_a_total_gets_the_ceiling_and_falls_back_to_the_nearest_shop_words(
    client, qa, ods_prices
):
    await seed({**OJJA, "ingredients": OJJA["ingredients"][:3]})
    placed = await ok(client, qa, "placeOrder", order_body(await recipe_id("ojja-merguez")))
    async with SessionLocal() as s:
        order = await s.get(Order, placed["order"]["id"])
    assert float(order.estimated_price) == 28 and float(order.budget_max) == 33
    assert (await reload_stop_name(order.id)) == "Supermarché ou épicerie le plus proche"


# --- admin ----------------------------------------------------------------------------------------------


async def test_admin_lists_publishes_unpublishes_and_imports(client, factory, qa):
    admin = await factory.user(role="admin")
    customer = await factory.user()
    report = await ok(
        client, admin, "importAdminRecipes", {"recipes": {"recipes": [OJJA]}, "ingredients": INGREDIENTS}
    )
    assert report["created"] == 1 and report["ingredients"]["created"] == len(INGREDIENTS)
    listed = await ok(client, admin, "listAdminRecipes")
    assert [(r["slug"], r["status"]) for r in listed["recipes"]] == [("ojja-merguez", "draft")]
    assert listed["public_allowed"] is False and listed["prices"]["available"] is False
    # admins-only for now: the admin browses the drafts too (owner 21/10)
    assert [r["slug"] for r in (await ok(client, qa, "listRecipes"))["recipes"]] == ["ojja-merguez"]
    rid = listed["recipes"][0]["id"]
    assert (
        await fn(client, customer, "setRecipeStatus", {"id": rid, "status": "published"})
    ).status_code == 403
    done = await ok(client, admin, "setRecipeStatus", {"id": rid, "status": "published"})
    assert done["status"] == "published" and done["published_at"]
    assert [r["slug"] for r in (await ok(client, qa, "listRecipes"))["recipes"]] == ["ojja-merguez"]
    await ok(client, admin, "setRecipeStatus", {"id": rid, "status": "draft"})
    assert [r["slug"] for r in (await ok(client, qa, "listRecipes"))["recipes"]] == [
        "ojja-merguez"
    ]  # draft, browsable
    async with SessionLocal() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity == "recipes"))).scalars().all()
        )
    assert actions == ["recipes.status", "recipes.status"]
    bulk = await ok(client, admin, "setRecipeStatus", {"ids": [rid], "status": "published"})
    assert bulk["changed"] == 1 and (await ok(client, qa, "listRecipes"))["total"] == 1


# --- the files' schema (21/10 vague 2: ~540 recipes, cuisine / pays, ingredient_new) ---------------------


def line(iid, fr, ar, qty, unit, placard=False, **kw):
    """One ingredient line as the recipe files write it."""
    return {
        "ingredient_id": iid,
        "nom_fr": fr,
        "nom_ar": ar,
        "quantite": qty,
        "unite": unit,
        "placard": placard,
        **kw,
    }


TAJINE = {
    "id": "tajine-zitoun",
    "statut": "brouillon",
    "nom_fr": "Tajine de poulet aux olives",
    "nom_ar": "طاجين الدجاج بالزيتون",
    "nom_local": {"ar": "طاجين زيتون", "latin": "Tajine zitoun"},
    "cuisine": "marocaine",
    "pays": "Maroc",
    "categorie": "tajine",
    "portions": 4,
    "prep_min": 15,
    "cuisson_min": 45,
    "ingredients": [
        line(3, "Oignon", "بصل", 0.3, "kg"),
        line(116, "Huile d'olive", "زيت", 3, "c. à soupe", placard=True),
        line(None, "Citron confit", "ليمون مصبر", 2, "pièce", ingredient_new="citron confit"),
        line(1, "Tomate", "طماطم", 2, "pièce"),
    ],
    "etapes_fr": ["Faites dorer l'oignon."],
    "etapes_ar": ["حمّروا البصل."],
    "astuce_fr": "Rincez le citron confit.",
    "sources": ["https://exemple.test/a", "https://exemple.test/b"],
}  # fmt: skip


async def test_files_schema_lines_without_reference_are_kept_unpriced_and_the_list_filters(
    client, qa, ods_prices
):
    doc = {"recettes": [TAJINE], "ingredients_a_ajouter": [{"key": "citron confit", "rayon": "epicerie"}]}
    async with transaction() as session:
        await recipes_import.import_ingredients(session, INGREDIENTS)
        report = await recipes_import.import_recipes(session, [doc, {"recettes": [OJJA, SALADE]}])
        await session.execute(text("UPDATE recipes SET status='published', published_at=now()"))
    assert report["created"] == 3 and report["refused"] == []
    assert any("Tomate" in w for w in report["warnings"])  # 2 pieces of a kg ingredient: kept, no price
    r = (await ok(client, qa, "getRecipe", {"slug": "tajine-zitoun"}))["recipe"]
    assert (r["cuisine"], r["country"], r["time_min"], r["tip_fr"]) == (
        "marocaine",
        "Maroc",
        60,
        "Rincez le citron confit.",
    )
    by_name = {i["name_fr"]: i for i in r["ingredients"]}
    assert (
        by_name["Citron confit"]["referenced"] is False
        and by_name["Citron confit"]["buy"]["price"]["priced"] is False
    )
    assert by_name["Huile d'olive"]["at_home"] is True
    assert {u["fr"] for u in r["cost"]["unpriced"]} == {"Citron confit", "Tomate"}
    first = await ok(client, qa, "listRecipes", {"page_size": 2})
    assert first["total"] == 3 and len(first["recipes"]) == 2 and first["has_more"] is True
    assert {"key": "marocaine", "count": 1} in first["facets"]["cuisines"]
    second = await ok(client, qa, "listRecipes", {"page_size": 2, "page": 2})
    assert len(second["recipes"]) == 1 and "facets" not in second
    assert [
        x["slug"] for x in (await ok(client, qa, "listRecipes", {"cuisine": "marocaine"}))["recipes"]
    ] == ["tajine-zitoun"]
    assert [x["slug"] for x in (await ok(client, qa, "listRecipes", {"q": "طاجين"}))["recipes"]] == [
        "tajine-zitoun"
    ]
    assert [x["slug"] for x in (await ok(client, qa, "listRecipes", {"q": "ZITOUN"}))["recipes"]] == [
        "tajine-zitoun"
    ]
    assert [x["slug"] for x in (await ok(client, qa, "listRecipes", {"max_time": 20}))["recipes"]] == [
        "salade-tomate"
    ]
    p = await ok(client, qa, "previewRecipeList", {"items": [{"recipe_id": r["id"], "servings": 4}]})
    citron = next(line for line in p["lines"] if line["name_fr"] == "Citron confit")
    assert citron["ingredient_id"] == "n:citron confit" and citron["text_fr"] == "2\u00a0pièces"
    async with transaction() as session:  # the dictionary grew: the same line now has its reference
        extra = [ref(151, "citron confit", "Citron confit", "ليمون مصبر", "piece", "epicerie")]
        await recipes_import.import_ingredients(session, extra)
        again = await recipes_import.import_recipes(session, [doc])
    assert again["updated"] == 1 and again["back_to_draft"] == ["tajine-zitoun"]
