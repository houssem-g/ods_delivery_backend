"""Vitrine paid extras (17/10, CONTRAT-AJOUTS): only the dish's questions in ODS — catalog intake
(fingerprint unchanged without them) → getVitrine `options.extras` → placeOrder (server prices, one-choice
groups, refusals) → items_text / cart → the ODS webhook (`unit_price` with the extras, `extras[]`)."""

import pytest
from sqlalchemy import select

from app.models import Order, OrderOffer
from app.services import vitrine_extras as ve
from tests.order_helpers import OrderWorld, reload, rows
from tests.partner_helpers import PARTNER_API, WEBHOOK_URL, make_partner
from tests.vitrine_helpers import (
    QA_COURIER,
    QA_CUSTOMER,
    cart_order,
    catalog_body,
    fn,
    manual_vitrine,
    ok,
    shop_body,
)

SUPPLEMENTS = {
    "id": "q71",
    "name": "Suppléments",
    "multiple": True,
    "choices": [
        {"id": "q71-aaaaaaaaaa", "name": "Fromage", "price": 1.5},
        {"id": "q71-bbbbbbbbbb", "name": "Œuf", "price": "1"},
    ],
}
SAUCE = {
    "id": "q72",
    "name": "Sauce",
    "multiple": False,
    "choices": [
        {"id": "q72-cccccccccc", "name": "Harissa", "price": 0},
        {"id": "q72-dddddddddd", "name": "Algérienne", "price": 0.5},
    ],
}
BURGER = {
    "ods_menu_id": 601,
    "ods_category_id": 12,
    "name": "Burger Maison + Frites",
    "price": 20.5,
    "position": 0,
    "ingredients": [{"name": "Oignons"}, {"name": "Mayonnaise"}],
    "extras": [SUPPLEMENTS, SAUCE],
}
COFFEE = {"ods_menu_id": 602, "ods_category_id": 12, "name": "Espresso", "price": 3.2, "position": 1}
MENU = [BURGER, COFFEE]


# --- pure --------------------------------------------------------------------------------------------


def test_clean_groups_cuts_and_cleans():
    groups = ve.clean_groups(
        [
            SUPPLEMENTS,
            {"id": "q73", "name": "Vide", "multiple": False, "choices": []},  # no choice: dropped
            {"id": "q74", "name": " Taille\x1b ", "choices": [
                {"id": "q74-a", "name": "Grand", "price": 2},
                {"id": "q74-b", "name": "grand", "price": 3},  # the same name twice: the first wins
                {"id": "q74-c", "name": "Énorme", "price": -1},  # negative: dropped
                {"id": "q74-d", "name": "Petit", "price": "x"},  # unreadable: dropped
                {"id": "bad id!", "name": "Moyen", "price": 1},  # not a key: dropped
            ]},
            {"id": "q71", "name": "Doublon", "choices": [{"id": "q71-z", "name": "Z", "price": 1}]},
        ]
    )  # fmt: skip
    assert [g["id"] for g in groups] == ["q71", "q74"]
    assert groups[0]["choices"][1] == {"id": "q71-bbbbbbbbbb", "name": "Œuf", "price": "1.000"}
    assert groups[1] == {
        "id": "q74", "name": "Taille", "multiple": False,
        "choices": [{"id": "q74-a", "name": "Grand", "price": "2.000"}],
    }  # fmt: skip
    many = [
        {"id": f"q{i}", "name": f"G{i}",
         "choices": [{"id": f"q{i}-{j}", "name": f"C{j}", "price": 1} for j in range(30)]}
        for i in range(12)
    ]  # fmt: skip
    cut = ve.clean_groups(many)
    assert len(cut) == ve.MAX_GROUPS and all(len(g["choices"]) == ve.MAX_CHOICES for g in cut)


def test_resolve_and_text():
    groups = ve.clean_groups([SUPPLEMENTS, SAUCE])
    chosen, bad = ve.resolve(["q72-cccccccccc", "q71-aaaaaaaaaa"], groups)
    assert bad == [] and [e["name"] for e in chosen] == ["Fromage", "Harissa"]  # the dish's order
    assert ve.total(chosen) == 1.5
    assert ve.text(chosen) == "+ Fromage (1.500 DT) · + Harissa"
    assert ve.resolve(["q71-zzzz"], groups) == ([], ["q71-zzzz"])
    # one-choice group: two sauces refused (both reported)
    assert ve.resolve(["q72-cccccccccc", "q72-dddddddddd"], groups)[1] == ["q72-cccccccccc", "q72-dddddddddd"]
    assert ve.parse(None) == [] and ve.parse(["a", "a"]) is None and ve.parse("q71") is None
    assert ve.parse(["x"] * 13) is None and ve.parse([1]) is None


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


async def linked(client, world, menu=MENU):
    pw = await make_partner(client, world.admin, webhook_url=WEBHOOK_URL)
    r = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(menu), headers=pw.headers)
    assert r.status_code == 200 and r.json()["status"] == "updated", r.text
    created = await ok(
        client,
        world.admin,
        "createVitrine",
        {
            "shop": shop_body("Coffee and Tea", category="restaurant"),
            "source": "ods",
            "partner_id": str(pw.partner.id),
        },
    )
    shop_id = created["vitrine"]["shop_id"]
    await ok(client, world.admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    admin = await ok(client, world.admin, "getVitrineAdmin", {"shop_id": shop_id})
    return pw, shop_id, {p["ods_menu_id"]: p for p in admin["products"]}


async def test_the_client_reads_only_what_ods_sent(client, world, qa):
    pw, shop_id, _products = await linked(client, world)
    seen = {
        p["name"]: p
        for p in (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]["products"]
    }
    burger = seen["Burger Maison + Frites"]["options"]["extras"]
    assert burger == [
        {"key": "q71", "name": {"fr": "Suppléments", "ar": "Suppléments"}, "multiple": True, "choices": [
            {"key": "q71-aaaaaaaaaa", "name": {"fr": "Fromage", "ar": "Fromage"}, "price": 1.5},
            {"key": "q71-bbbbbbbbbb", "name": {"fr": "Œuf", "ar": "Œuf"}, "price": 1.0}]},
        {"key": "q72", "name": {"fr": "Sauce", "ar": "Sauce"}, "multiple": False, "choices": [
            {"key": "q72-cccccccccc", "name": {"fr": "Harissa", "ar": "Harissa"}, "price": 0.0},
            {"key": "q72-dddddddddd", "name": {"fr": "Algérienne", "ar": "Algérienne"}, "price": 0.5}]},
    ]  # fmt: skip
    assert seen["Espresso"]["options"]["extras"] == []  # no question in ODS: nothing
    # the same menu again: unchanged (the extras are in the fingerprint)
    r = await client.put(
        f"{PARTNER_API}/catalog", json=catalog_body(MENU, version="sha256:v2"), headers=pw.headers
    )
    assert r.json()["status"] == "unchanged"
    # the restaurant removed its questions in ODS: no extra any more
    plain = [{k: v for k, v in p.items() if k != "extras"} for p in MENU]
    r = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(plain), headers=pw.headers)
    assert r.json()["status"] == "updated"
    seen = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]["products"]
    assert all(p["options"]["extras"] == [] for p in seen)


async def test_a_menu_without_extras_keeps_its_fingerprint(client, world):
    """An ODS that sends no `extras` (or an empty list) fingerprints exactly as before."""
    from app.services import vitrine_catalog as vc

    plain = catalog_body([COFFEE])
    with_empty = catalog_body([{**COFFEE, "extras": []}])
    a = vc.content_hash(vc.normalized(vc.parse(plain)))
    b = vc.content_hash(vc.normalized(vc.parse(with_empty)))
    assert a == b and "extras" not in vc.normalized(vc.parse(plain))["products"][0]


async def test_a_hand_made_vitrine_has_no_extra(client, world, qa):
    shop_id, _ids = await manual_vitrine(client, world.admin)
    seen = (await ok(client, qa, "getVitrine", {"shop_id": shop_id}))["vitrine"]["products"]
    assert all(p["options"]["extras"] == [] for p in seen)


async def test_an_order_with_extras(client, world, qa, qa_courier):
    from tests.test_vitrine_orders import deliveries

    _pw, shop_id, products = await linked(client, world)
    burger, coffee = products[601]["id"], products[602]["id"]
    lines = [
        {
            "product_id": burger, "qty": 2, "unit_price": 23.0, "note": "bien cuit",
            "mods": [{"key": "oignon", "level": "none"}],
            "extras": ["q71-aaaaaaaaaa", "q71-bbbbbbbbbb", "q72-cccccccccc"],
        },
        {"product_id": burger, "qty": 1, "unit_price": 20.5},  # the same dish, as it is: its own line
        {"product_id": coffee, "qty": 1, "unit_price": 3.2},
    ]  # fmt: skip
    r = await fn(client, qa, "placeOrder", cart_order(shop_id, lines))
    assert r.status_code == 200, r.text
    order = await reload(Order, r.json()["order"]["id"])
    assert order.items_text == (
        "QA TEST [Vitrine] Coffee and Tea\n"
        "2× Burger Maison + Frites — 46.000 DT · Sans oignon · + Fromage (1.500 DT) · + Œuf (1.000 DT)"
        " · + Harissa (bien cuit)\n"
        "1× Burger Maison + Frites — 20.500 DT\n"
        "1× Espresso — 3.200 DT\n"
        "Total articles : 69.700 DT"
    )
    assert float(order.budget_max) == 69.7 and float(order.estimated_price) == 69.7
    first = order.cart["lines"][0]
    assert (
        first["unit_price"] == "23.000"
        and first["base_price"] == "20.500"
        and first["line_total"] == "46.000"
    )
    assert first["extras"][0] == {
        "key": "q71-aaaaaaaaaa", "group": "q71", "group_name": "Suppléments", "name": "Fromage",
        "price": "1.500",
    }  # fmt: skip
    assert first["extras_text"] == "+ Fromage (1.500 DT) · + Œuf (1.000 DT) · + Harissa"
    assert "extras" not in order.cart["lines"][1] and "base_price" not in order.cart["lines"][1]
    # ODS receives the extras, the line's unit price with them
    await ok(client, qa_courier, "createOrderOffer", {"order_id": str(order.id), "fee": 4, "eta_minutes": 15})
    offer_id = (await rows(select(OrderOffer)))[0].id
    await ok(client, qa, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(offer_id)})
    [confirmed] = await deliveries(order.id)
    sent = confirmed.payload["data"]
    assert sent["items_total"] == 69.7
    line = sent["lines"][0]
    assert line["unit_price"] == 23.0 and line["base_price"] == 20.5 and line["mods_text"] == "Sans oignon"
    sup = {"group_id": "q71", "question": "Suppléments"}
    assert line["extras"] == [
        {**sup, "choice_id": "q71-aaaaaaaaaa", "name": "Fromage", "price": 1.5},
        {**sup, "choice_id": "q71-bbbbbbbbbb", "name": "Œuf", "price": 1.0},
        {"group_id": "q72", "question": "Sauce", "choice_id": "q72-cccccccccc", "name": "Harissa",
         "price": 0.0},
    ]  # fmt: skip
    assert line["extras_text"] == "+ Fromage (1.500 DT) · + Œuf (1.000 DT) · + Harissa"
    assert "extras" not in sent["lines"][1] and "base_price" not in sent["lines"][1]


async def test_extras_are_checked_by_the_server(client, world, qa):
    pw, shop_id, products = await linked(client, world)
    burger, coffee = products[601]["id"], products[602]["id"]

    async def place(lines):
        return await fn(client, qa, "placeOrder", cart_order(shop_id, lines))

    # unknown, another dish's, or two choices in a one-choice group → options_changed
    for keys, pid in (
        (["q71-zzzzzzzzzz"], burger),
        (["q71-aaaaaaaaaa"], coffee),
        (["q72-cccccccccc", "q72-dddddddddd"], burger),
    ):
        r = await place([{"product_id": pid, "qty": 1, "extras": keys}])
        assert r.status_code == 409 and r.json()["error"] == "options_changed", r.text
        assert r.json()["lines"][0]["product_id"] == pid and set(keys) <= set(r.json()["lines"][0]["keys"])
    # the client's price is never taken: a cheese at 0 → prices_changed with the price of one (dish + extras)
    r = await place([{"product_id": burger, "qty": 1, "unit_price": 20.5, "extras": ["q71-aaaaaaaaaa"]}])
    assert r.status_code == 409 and r.json()["error"] == "prices_changed", r.text
    row = r.json()["lines"][0]
    assert (row["old_price"], row["new_price"], row["base_price"]) == (20.5, 22.0, 20.5)
    assert row["extras"] == [{"key": "q71-aaaaaaaaaa", "price": 1.5}]
    # two lines of the same dish (one with extras, one without), both priced wrong: one row for the dish,
    # its own price apart (`base_price`) and the extras' prices, whatever the order of the lines
    plain_line = {"product_id": burger, "qty": 1, "unit_price": 1}
    cheese_line = {**plain_line, "extras": ["q71-aaaaaaaaaa"]}
    for lines in ([plain_line, cheese_line], [cheese_line, plain_line]):
        r = await place(lines)
        assert r.status_code == 409 and r.json()["error"] == "prices_changed", r.text
        [row] = r.json()["lines"]
        assert row["base_price"] == 20.5 and row["extras"] == [{"key": "q71-aaaaaaaaaa", "price": 1.5}]
    # a bad shape, or the same dish + removals + extras twice
    same = {"product_id": burger, "qty": 1, "extras": ["q71-aaaaaaaaaa"]}
    for lines in (
        [{"product_id": burger, "qty": 1, "extras": "q71-aaaaaaaaaa"}],
        [{"product_id": burger, "qty": 1, "extras": ["q71-aaaaaaaaaa", "q71-aaaaaaaaaa"]}],
        [same, {**same, "qty": 2}],
    ):
        r = await place(lines)
        assert r.status_code == 400 and r.json()["error"] == "invalid_cart", r.text
    # the same extras in another order = the same line identity
    two = {"product_id": burger, "qty": 1, "extras": ["q71-aaaaaaaaaa", "q71-bbbbbbbbbb"]}
    r = await place([two, {**two, "extras": ["q71-bbbbbbbbbb", "q71-aaaaaaaaaa"], "qty": 3}])
    assert r.status_code == 400
    assert await rows(select(Order)) == []
    # the restaurant changes the cheese's price in ODS: the next order pays the new one
    menu = [
        {**BURGER, "extras": [{**SUPPLEMENTS, "choices": [{**SUPPLEMENTS["choices"][0], "price": 2}]}]},
        COFFEE,
    ]
    r = await client.put(f"{PARTNER_API}/catalog", json=catalog_body(menu), headers=pw.headers)
    assert r.json()["status"] == "updated"
    r = await place([{"product_id": burger, "qty": 1, "extras": ["q71-aaaaaaaaaa"]}])
    assert r.status_code == 200, r.text
    assert (await rows(select(Order)))[0].cart["lines"][0]["unit_price"] == "22.500"
    # the egg is gone from ODS: refused
    r = await place([{"product_id": burger, "qty": 1, "extras": ["q71-bbbbbbbbbb"]}])
    assert r.status_code == 409 and r.json()["error"] == "options_changed"
