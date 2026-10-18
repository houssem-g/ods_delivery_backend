"""Vitrine « ce que j'enlève » (17/10): the engine (no AI, deterministic) on the 88 dishes of Coffee and
Tea and on a Tunisian snack, then the whole path: catalog with the ODS recipe → getVitrine → placeOrder
(server-side check, two lines of one dish) → items_text, cart, the ODS webhook → the learning → the
admin's corrections."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import transaction
from app.models import Order, OrderOffer, VitrineOptionStat
from app.services import vitrine_options as vo
from tests.order_helpers import OrderWorld, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, make_partner
from tests.vitrine_helpers import QA_COURIER, QA_CUSTOMER, cart_order, catalog_body, fn, ok, shop_body

DATA = Path(__file__).parent / "data"


def labels(name, description=None, category=None, ingredients=None, kind="restaurant"):
    recipe = [{"name": n, "removable": None} for n in ingredients] if ingredients is not None else None
    return [
        "/".join(o["labels"][lv]["fr"] for lv in o["levels"])
        for o in vo.rank(vo.candidates(name, description, category, recipe, kind))
    ]


# --- the engine --------------------------------------------------------------------------------------


COFFEE_AND_TEA = {
    "Espresso": ["Peu de sucre/Sans sucre"],
    "Cappuccino": ["Peu de sucre/Sans sucre", "Peu de cacao/Sans cacao"],
    "Cappuccino Chantilly": ["Peu de sucre/Sans sucre"],
    "Latte Macchiato": ["Peu de sucre/Sans sucre"],
    "Chocolat Chaud": ["Peu de chantilly/Sans chantilly"],
    "Iced Latte": ["Peu de sucre/Sans sucre", "Peu de glaçons/Sans glaçons"],
    "Thé à la Menthe": ["Peu de sucre/Sans sucre"],
    "Thé aux Pignons": ["Peu de sucre/Sans sucre", "Peu de menthe/Sans menthe"],
    "Citronnade": ["Peu de sucre/Sans sucre", "Peu de glaçons/Sans glaçons"],
    "Jus d'Orange": ["Peu de glaçons/Sans glaçons"],
    "Banane Fraise": ["Peu de sucre/Sans sucre", "Peu de glaçons/Sans glaçons", "Peu de lait/Sans lait"],
    "Milkshake Vanille": [],
    "Omelette Fromage": [],
    "Crêpe Nutella": [],
    "Crêpe Jambon Fromage Champignons": [
        "Peu de fromage/Sans fromage",
        "Peu de champignons/Sans champignons",
    ],
    "Mini Sandwich Thon ou Omelette": ["Peu de harissa/Sans harissa"],
    "Sandwich Thon, Œufs, Mayonnaise": [
        "Peu de harissa/Sans harissa",
        "Peu de mayonnaise/Sans mayonnaise",
        "Peu de salade/Sans salade",
        "Peu d'œuf/Sans œuf",
    ],
    "Sandwich Caprese": [],
    "French Tacos Escalope": [
        "Peu de harissa/Sans harissa",
        "Peu de frites/Sans frites",
        "Peu de fromage/Sans fromage",
    ],
    "Pizza Margherita": [],
    "Pizza Thon et Jambon": ["Peu d'olives/Sans olives"],
    "Pizza Quatre Fromages": [],
    "Salade Burrata": [
        "Peu de tomate/Sans tomate",
        "Peu de basilic/Sans basilic",
        "Peu de roquette/Sans roquette",
    ],
    "Salade de Fruits": ["Peu de miel/Sans miel"],
    "Penne Bolognaise": ["Peu d'oignon/Sans oignon", "Peu de fromage/Sans fromage"],
    "Pâtes Cacio e Pepe": [],
    "Burger Maison + Frites": [
        "Peu d'oignon/Sans oignon",
        "Peu de mayonnaise/Sans mayonnaise",
        "Peu de salade/Sans salade",
        "Peu de fromage/Sans fromage",
        "Peu de tomate/Sans tomate",
    ],
    "Crispy Chicken Burger Piquant": [
        "Peu de harissa/Sans harissa",
        "Peu de mayonnaise/Sans mayonnaise",
        "Peu de frites/Sans frites",
        "Peu de salade/Sans salade",
    ],
    "Double Cheese Burger": [
        "Peu d'oignon/Sans oignon",
        "Peu de mayonnaise/Sans mayonnaise",
        "Peu de frites/Sans frites",
    ],
    "Tiramisu Classique": [],
}
PACKAGING_AND_BASES = {
    "Serviette papier",
    "Paille",
    "Café en grains",
    "Farine",
    "Pain baguette",
    "Pain burger",
}


def test_coffee_and_tea_88_dishes():
    dishes = json.loads((DATA / "vitrine_options_coffee_tea.json").read_text())
    assert len(dishes) == 88
    got = {d["name"]: labels(d["name"], None, d["category"], d["ingredients"], "cafe") for d in dishes}
    for name, expected in COFFEE_AND_TEA.items():
        assert got[name] == expected, name
    for d in dishes:
        options = vo.candidates(
            d["name"], None, d["category"], [{"name": n} for n in d["ingredients"]], "cafe"
        )
        assert len(vo.rank(options)) <= vo.MAX_SHOWN
        for o in options:  # only the lexicon, never a base nor a packaging
            assert o["key"] in vo.BY_KEY
            assert o["name"]["fr"] not in PACKAGING_AND_BASES
    assert sum(1 for v in got.values() if v) >= 60  # most dishes get something to remove


def test_a_tunisian_snack_without_recipes():
    # every removal has « Peu » and « Sans »; harissa where a Tunisian counter serves it, said or not
    assert labels("Makloub thon", category="Makloub") == [
        "Peu de harissa/Sans harissa",
        "Peu d'oignon/Sans oignon",
        "Peu de mayonnaise/Sans mayonnaise",
        "Peu de frites/Sans frites",
        "Peu de salade/Sans salade",
        "Peu de fromage/Sans fromage",
    ]
    assert labels("Kaskrout thon", "Baguette, thon, harissa, olives, œuf, câpres") == [
        "Peu de harissa/Sans harissa",
        "Peu d'olives/Sans olives",
        "Peu d'œuf/Sans œuf",
        "Peu de câpres/Sans câpres",
    ]
    assert labels("Libanais poulet", "Poulet mariné, fromage, frites, sauce blanche, tomate") == [
        "Peu de harissa/Sans harissa",
        "Peu de frites/Sans frites",
        "Peu de fromage/Sans fromage",
        "Peu de tomate/Sans tomate",
        "Peu de sauce blanche/Sans sauce blanche",
    ]
    assert labels("Pizza 4 saisons", "Champignons, jambon, poivrons, olives, fromage") == [
        "Peu d'olives/Sans olives",
        "Peu de champignons/Sans champignons",
        "Peu de poivron/Sans poivron",
    ]
    assert labels("Ojja merguez", "Œufs, sauce tomate, piment, merguez") == [
        "Peu de harissa/Sans harissa",
        "Peu de piment/Sans piment",
    ]
    assert labels("Couscous agneau") == ["Peu de harissa/Sans harissa", "Peu de piment/Sans piment"]
    assert labels("Baguette farcie merguez", "Merguez, frites, oignon") == [
        "Peu de harissa/Sans harissa",
        "Peu d'oignon/Sans oignon",
        "Peu de frites/Sans frites",
    ]
    assert labels("Pizza margherita", "Sauce tomate, mozzarella, basilic") == []
    assert labels("Pizza tunisienne") == ["Peu d'olives/Sans olives"]  # a pizza: harissa only when named
    assert labels("Pizza piquante", "Sauce tomate, harissa, thon")[0] == "Peu de harissa/Sans harissa"
    assert labels("Lablabi")[:3] == [
        "Peu de harissa/Sans harissa",
        "Peu d'olives/Sans olives",
        "Peu de thon/Sans thon",
    ]
    assert labels("Burger maison")[0] == "Peu d'oignon/Sans oignon"  # a burger: no harissa unless « piquant »
    assert labels("Burger piquant")[0] == "Peu de harissa/Sans harissa"
    # never on a coffee, a drink, a dessert
    assert labels("Café direct", category="Boissons") == ["Peu de sucre/Sans sucre"]
    assert labels("Citronnade menthe 1 L") == ["Peu de sucre/Sans sucre", "Peu de glaçons/Sans glaçons"]
    assert labels("Bambalouni") == ["Peu de sucre/Sans sucre"]
    assert labels("Tiramisu") == [] and labels("Sandwich Caprese", "Mozzarella, tomate, basilic") == []
    for bottle in ("Coca-Cola 33 cl", "Eau minérale 1,5 L", "Boga cidre", "Canette Fanta", "Yaourt nature"):
        assert labels(bottle, category="Boissons") == [], bottle
    assert labels("The Burger maison")[0] == "Peu d'oignon/Sans oignon"  # a burger, never a tea
    assert not any(
        "oignon" in x for x in labels("Assiette grill sans oignons", "Kefta, frites, salade, sans oignons")
    )
    # Arabic names
    assert labels("مقلوب دجاج", "دجاج، جبن، بطاطا مقلية، هريسة")[:2] == [
        "Peu de harissa/Sans harissa",
        "Peu de frites/Sans frites",
    ]
    assert labels("عصير ليمون") == ["Peu de sucre/Sans sucre", "Peu de glaçons/Sans glaçons"]
    # a supermarket's products get nothing
    assert labels("Makloub thon", kind="supermarket") == []
    every = json.loads((DATA / "vitrine_options_snack.json").read_text())
    for d in every:
        options = vo.candidates(d["name"], d["description"], d["category"], None, "restaurant")
        assert all(o["levels"] == ["less", "none"] for o in options), d["name"]
        assert len(vo.rank(options)) <= 6


def test_labels_fr_and_ar():
    [harissa] = vo.candidates("Pizza", "harissa, pâte", ingredients=[{"name": "Harissa"}])
    assert harissa["labels"] == {
        "less": {"fr": "Peu de harissa", "ar": "قليل من الهريسة"},
        "none": {"fr": "Sans harissa", "ar": "بدون هريسة"},
    }
    [ail] = vo.candidates("Pizza", ingredients=[{"name": "Ail frais"}])
    assert ail["labels"]["less"]["fr"] == "Peu d'ail" and ail["labels"]["none"]["ar"] == "بدون ثوم"
    [oignon] = vo.candidates("Pizza", ingredients=[{"name": "Oignons"}])
    assert oignon["levels"] == ["less", "none"]
    assert oignon["labels"]["less"] == {"fr": "Peu d'oignon", "ar": "قليل من البصل"}
    [oeuf] = vo.candidates("Pizza", ingredients=[{"name": "Œufs"}])
    assert oeuf["labels"]["less"]["fr"] == "Peu d'œuf"


def test_the_restaurant_decides_in_ods():
    recipe = [
        {"name": "Oignons", "removable": False},  # « ne peut pas être retiré »
        {"name": "Harissa", "removable": None},
        {"name": "Graines de nigelle", "removable": True},  # unknown to the lexicon, but said removable
    ]
    keys = [o["key"] for o in vo.candidates("Sandwich escalope", ingredients=recipe)]
    assert "oignon" not in keys and keys[0] == "harissa"
    [custom] = [
        o for o in vo.candidates("Sandwich escalope", ingredients=recipe) if o["key"].startswith("o-")
    ]
    assert custom["labels"]["none"]["fr"] == "Sans graines de nigelle" and custom["levels"] == [
        "less",
        "none",
    ]


def test_ranking_learns_from_orders_then_category():
    options = vo.candidates("Makloub thon")
    assert [o["key"] for o in vo.rank(options)][:2] == ["harissa", "oignon"]
    learnt = vo.rank(options, {"frites": 3}, {"salade": 9})
    assert [o["key"] for o in learnt][:3] == ["frites", "salade", "harissa"]
    pinned = vo.rank(options, {"frites": 3}, None, ["fromage"])
    assert pinned[0]["key"] == "fromage" and len(pinned) == 6


def test_admin_override_shape():
    auto = {"harissa", "oignon"}
    assert vo.clean_override({"reset": True, "hidden": ["oignon"]}, auto) is None
    stored = vo.clean_override(
        {
            "hidden": ["oignon", "unknown"],
            "added": [{"name_fr": "Sauce piquante maison", "levels": ["less", "none"]}],
        },
        auto,
    )
    assert stored == {
        "hidden": ["oignon"],
        "added": [{"key": "c-sauce-piquante-maison", "name_fr": "Sauce piquante maison", "name_ar": None,
                   "levels": ["less", "none"]}],
        "order": [],
    }  # fmt: skip
    for bad in (
        {"added": [{"name_fr": ""}]},
        {"added": [{"name_fr": "X", "levels": ["more"]}]},
        {"hidden": "x"},
    ):
        with pytest.raises(Exception):  # noqa: B017
            vo.clean_override(bad, auto)


def test_mods_shape():
    assert vo.parse_mods(None) == []
    assert vo.parse_mods([{"key": "harissa", "level": "less"}]) == [{"key": "harissa", "level": "less"}]
    for bad in (
        "x",
        [{"key": "harissa", "level": "more"}],
        [{"key": "a", "level": "none"}] * 2,
        [1],
        [{}] * 9,
    ):
        assert vo.parse_mods(bad) is None


# --- the whole path ----------------------------------------------------------------------------------


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
async def qa(factory):
    return await factory.user(email=QA_CUSTOMER, full_name="Lemel Qa Client", phone_e164="+21622111333")


@pytest.fixture
async def qa_courier(factory, world):
    user = await factory.user(email=QA_COURIER, full_name="Vovine", profile=False)
    await world.make_courier(user, display_name="Vovine Livreur", phone_e164="+21655999888")
    return user


SNACK = [
    {"ods_menu_id": 501, "ods_category_id": 12, "name": "Makloub escalope", "price": 9.5, "position": 0,
     "ingredients": [{"name": "Pâte makloub"}, {"name": "Escalope de poulet"}, {"name": "Harissa"},
                     {"name": "Oignons", "removable": False}, {"name": "Frites surgelées"},
                     {"name": "Laitue (pièce)"}, {"name": "Mozzarella"}, {"name": "Serviette papier"}]},
    {"ods_menu_id": 502, "ods_category_id": 12, "name": "Citronnade", "price": 3.5, "position": 1},
    {"ods_menu_id": 503, "ods_category_id": 12, "name": "Eau minérale 1,5 L", "price": 1.2, "position": 2},
]  # fmt: skip


async def snack(client, world):
    pw = await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)
    body = catalog_body(SNACK)
    r = await client.put(f"{PARTNER_API}/catalog", json=body, headers=pw.headers)
    assert r.status_code == 200 and r.json()["status"] == "updated", r.text
    shop = shop_body("Snack Amel", category="restaurant")
    created = await ok(
        client,
        world.admin,
        "createVitrine",
        {"shop": shop, "source": "ods", "partner_id": str(pw.partner.id)},
    )
    shop_id = created["vitrine"]["shop_id"]
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    admin = await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id})
    return pw, shop_id, {p["ods_menu_id"]: p for p in admin["products"]}


def keys_of(product):
    return [(o["key"], o["levels"]) for o in product["options"]["remove"]]


async def test_the_recipe_travels_and_the_client_reads_the_options(client, world, qa):
    pw, shop_id, products = await snack(client, world)
    makloub = products[501]
    assert makloub["ingredients"][3] == {"name": "Oignons", "removable": False}
    assert keys_of(makloub) == [
        ("harissa", ["less", "none"]),
        ("frites", ["less", "none"]),
        ("salade", ["less", "none"]),
        ("fromage", ["less", "none"]),
    ]  # fmt: skip  (oignon: « ne peut pas être retiré » in ODS)
    assert keys_of(products[503]) == []
    seen = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]["products"]
    by_name = {p["name"]: p for p in seen}
    assert by_name["Citronnade"]["options"]["remove"][0]["labels"]["less"] == {
        "fr": "Peu de sucre", "ar": "قليل من السكر",
    }  # fmt: skip
    assert (
        "ingredients" not in by_name["Makloub escalope"] and "ods_menu_id" not in by_name["Makloub escalope"]
    )
    # the same push again: unchanged (the recipe is part of the fingerprint)
    r = await client.put(
        f"{PARTNER_API}/catalog", json=catalog_body(SNACK, version="sha256:v2"), headers=pw.headers
    )
    assert r.json()["status"] == "unchanged"
    # a catalog without any recipe keeps the fingerprint an older ODS gave
    plain = [{k: v for k, v in p.items() if k != "ingredients"} for p in SNACK]
    r = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(plain), headers=pw.headers)
    assert r.json()["status"] == "updated"
    admin = await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id})
    makloub = {p["ods_menu_id"]: p for p in admin["products"]}[501]
    assert makloub["ingredients"] is None and keys_of(makloub)[0] == ("harissa", ["less", "none"])


async def test_an_order_with_removals(client, world, qa, qa_courier):
    from tests.test_vitrine_orders import deliveries

    _pw, shop_id, products = await snack(client, world)
    makloub, citron = products[501]["id"], products[502]["id"]
    mods = [{"key": "harissa", "level": "less"}, {"key": "frites", "level": "none"}]
    lines = [
        {"product_id": makloub, "qty": 1, "mods": mods, "note": "bien chaud"},
        {"product_id": makloub, "qty": 1},  # the same dish, as usual: a second line
        {"product_id": citron, "qty": 2, "mods": [{"key": "sucre", "level": "none"}]},
    ]
    r = await fn(client, qa, "placeOrder", cart_order(shop_id, lines))
    assert r.status_code == 200, r.text
    order = await reload(Order, r.json()["order"]["id"])
    assert order.items_text == (
        "QA TEST [Vitrine] Snack Amel\n"
        "1× Makloub escalope — 9.500 DT · Peu de harissa · Sans frites (bien chaud)\n"
        "1× Makloub escalope — 9.500 DT\n"
        "2× Citronnade — 7.000 DT · Sans sucre\n"
        "Total articles : 26.000 DT"
    )
    assert float(order.budget_max) == 26.0  # a removal never changes the price
    first = order.cart["lines"][0]
    assert first["mods_text"] == "Peu de harissa · Sans frites"
    assert first["mods"][0] == {
        "key": "harissa", "level": "less", "label_fr": "Peu de harissa", "label_ar": "قليل من الهريسة",
        "price_delta": "0.000",
    }  # fmt: skip
    assert "mods" not in order.cart["lines"][1]
    doc = r.json()["order"]["vitrine_cart"]["lines"]
    assert doc[0]["mods_text"] == "Peu de harissa · Sans frites" and "ods_menu_id" not in doc[0]
    # a test order (QA) never feeds the chips' order
    assert await rows(select(VitrineOptionStat)) == []
    # a real order does: aggregated counts, no customer, then the most removed comes first
    async with transaction() as session:
        await vo.record(
            session, uuid.UUID(shop_id), [{"product_id": makloub, "mods": [{"key": "salade"}]}] * 2
        )
    stats = {(str(s.product_id), s.key): s.count for s in await rows(select(VitrineOptionStat))}
    assert stats == {(makloub, "salade"): 2}
    seen = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]["products"]
    first_chip = {p["name"]: p["options"]["remove"][0]["key"] for p in seen if p["options"]["remove"]}
    assert first_chip["Makloub escalope"] == "salade"
    # ODS receives the removals of each line
    await ok(client, qa_courier, "createOrderOffer", {"order_id": str(order.id), "fee": 4, "eta_minutes": 15})
    offer_id = (await rows(select(OrderOffer)))[0].id
    await ok(client, qa, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(offer_id)})
    [confirmed] = await deliveries(order.id)
    sent = confirmed.payload["data"]["lines"]
    assert sent[0]["mods"] == [
        {"key": "harissa", "level": "less", "label": "Peu de harissa"},
        {"key": "frites", "level": "none", "label": "Sans frites"},
    ]
    assert sent[0]["mods_text"] == "Peu de harissa · Sans frites" and sent[0]["note"] == "bien chaud"
    assert "mods" not in sent[1] and sent[2]["mods_text"] == "Sans sucre"


async def test_removals_are_checked_by_the_server(client, world, qa):
    _pw, shop_id, products = await snack(client, world)
    makloub, eau = products[501]["id"], products[503]["id"]

    async def place(lines):
        return await fn(client, qa, "placeOrder", cart_order(shop_id, lines))

    # unknown to this dish (the oignon is « never » in ODS, the water has nothing, the sugar is no makloub's)
    for mods, pid in (
        ([{"key": "oignon", "level": "none"}], makloub),
        ([{"key": "oignon", "level": "less"}], makloub),
        ([{"key": "sucre", "level": "none"}], eau),
        ([{"key": "sucre", "level": "less"}], makloub),
        ([{"key": "<script>", "level": "none"}], makloub),
    ):
        r = await place([{"product_id": pid, "qty": 1, "mods": mods}])
        assert r.status_code == 409 and r.json()["error"] == "options_changed", r.text
        assert r.json()["lines"][0]["product_id"] == pid
    # a bad shape, or the same dish with the same removals twice
    same = {"product_id": makloub, "qty": 1, "mods": [{"key": "harissa", "level": "none"}]}
    for lines in (
        [{"product_id": makloub, "qty": 1, "mods": [{"key": "harissa", "level": "max"}]}],
        [{"product_id": makloub, "qty": 1, "mods": "sans harissa"}],
        [same, {**same, "qty": 2}],
    ):
        r = await place(lines)
        assert r.status_code == 400 and r.json()["error"] == "invalid_cart", r.text
    # 50 of a dish at most, even split over lines with other removals
    split = [
        {"product_id": makloub, "qty": 30, "mods": [{"key": "harissa", "level": "none"}]},
        {"product_id": makloub, "qty": 30},
    ]
    r = await place(split)
    assert r.status_code == 400 and r.json()["error"] == "invalid_cart", r.text
    assert await rows(select(Order)) == []
    # the free note stays bounded (120), without any control character (it reaches a kitchen printer)
    r = await place([{"product_id": makloub, "qty": 1, "note": "bien\x1bp\x00 cuit"}])
    assert r.status_code == 200 and (await rows(select(Order)))[0].cart["lines"][0]["note"] == "bienp cuit"
    # the free note stays bounded (120)
    r = await place([{"product_id": makloub, "qty": 1, "note": "x" * 500}])
    assert r.status_code == 200
    assert max(len(o.cart["lines"][0]["note"]) for o in await rows(select(Order))) == 120


async def test_the_admin_corrects_the_options(client, world, qa):
    _pw, shop_id, products = await snack(client, world)
    makloub = products[501]["id"]
    body = {"shop_id": shop_id, "product_id": makloub}
    r = await fn(client, qa, "setVitrineProductOptions", {**body, "hidden": ["frites"]})
    assert r.status_code == 403
    product = (
        await ok(
            client,
            world.admin,
            "setVitrineProductOptions",
            {**body, "hidden": ["frites"], "added": [{"name_fr": "Sauce maison", "name_ar": "صلصة البيت",
                                                      "levels": ["less", "none"]}]},
        )
    )["product"]  # fmt: skip
    assert [o["key"] for o in product["options"]["remove"]] == [
        "c-sauce-maison",
        "harissa",
        "salade",
        "fromage",
    ]
    assert product["options"]["remove"][0]["labels"]["less"] == {
        "fr": "Peu de sauce maison",
        "ar": "كمية قليلة من صلصة البيت",
    }
    assert {o["key"] for o in product["options_auto"]} >= {"frites", "harissa"}
    # the customer sees it, and can order it; a hidden one is refused
    ok_line = {"product_id": makloub, "qty": 1, "mods": [{"key": "c-sauce-maison", "level": "less"}]}
    assert (await fn(client, qa, "placeOrder", cart_order(shop_id, [ok_line]))).status_code == 200
    hidden = {"product_id": makloub, "qty": 1, "mods": [{"key": "frites", "level": "none"}]}
    assert (await fn(client, qa, "placeOrder", cart_order(shop_id, [hidden]))).json()[
        "error"
    ] == "options_changed"
    # pinned order, then back to automatic
    product = (await ok(client, world.admin, "setVitrineProductOptions", {**body, "order": ["fromage"]}))[
        "product"
    ]
    assert product["options"]["remove"][0]["key"] == "fromage" and product["options_override"]["hidden"] == []
    product = (await ok(client, world.admin, "setVitrineProductOptions", {**body, "reset": True}))["product"]
    assert product["options_override"] is None and product["options"]["remove"][1]["key"] == "frites"
    r = await fn(client, world.admin, "setVitrineProductOptions", {**body, "added": [{"name_fr": ""}]})
    assert r.status_code == 400 and r.json()["error"] == "validation_error"
