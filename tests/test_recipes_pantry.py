"""« Avec ce que j'ai » (owner 21/10): which recipes the customer can make with what he has — exact rules on
the server (app/services/recipes_pantry.py). One short test per rule: all there, 1 missing, optional ignored,
the placard, darija words and synonyms, families, the filters, admins only."""

import copy

from app.models import Ingredient
from app.services import recipes_pantry as rp
from tests.test_recipes import OJJA, SALADE, ods_prices, qa, seed  # noqa: F401  (fixtures)
from tests.vitrine_helpers import fn, ok

MATCH = "matchRecipesByPantry"


def groups(answer):
    """{missing: [slugs]} of the non-empty groups."""
    return {g["missing"]: [r["slug"] for r in g["recipes"]] for g in answer["groups"] if g["recipes"]}


def card(answer, slug):
    return next(r for g in answer["groups"] for r in g["recipes"] if r["slug"] == slug)


async def test_everything_there_one_missing_and_the_cost_of_what_is_missing_only(client, qa, ods_prices):  # noqa: F811
    await seed(OJJA, SALADE)
    a = await ok(client, qa, MATCH, {"have": [1, 3]})  # tomate, oignon (+ huile d'olive in the placard)
    assert groups(a) == {0: ["salade-tomate"], 3: ["ojja-merguez"]} and a["total"] == 2
    assert card(a, "salade-tomate")["missing"] == [] and card(a, "salade-tomate")["have_n"] == 2
    b = await ok(client, qa, MATCH, {"have": [41, 1, 77]})  # merguez, tomate, oeufs → ojja without persil
    assert groups(b) == {1: ["ojja-merguez", "salade-tomate"]}  # 3 of his ingredients before 1 (tomate)
    ojja = card(b, "ojja-merguez")
    assert ojja["missing"] == [{"id": 17, "name_fr": "Persil", "name_ar": "معدنوس"}] and ojja["have_n"] == 3
    # « Me faire livrer ce qui manque »: « Ma liste » gets these as « j'ai déjà » and buys persil only
    assert {41, 1, 77, 116} <= set(ojja["at_home"]) and 17 not in ojja["at_home"]
    assert ojja["missing_cost"]["unpriced"] == [{"id": 17, "fr": "Persil", "ar": "معدنوس"}]
    c = await ok(client, qa, MATCH, {"have": [1]})  # tomate alone: ojja misses 3 (merguez, oeufs, persil)
    assert groups(c) == {1: ["salade-tomate"], 3: ["ojja-merguez"]}
    assert (await ok(client, qa, MATCH, {"have": [17]}))["groups"][3]["recipes"][0]["slug"] == "ojja-merguez"
    assert (await ok(client, qa, MATCH, {"have": []}))["total"] == 0


async def test_an_optional_line_never_counts_as_missing(client, qa, ods_prices):  # noqa: F811
    ojja = copy.deepcopy(OJJA)
    ojja["ingredients"][3]["optional"] = True  # persil « optionnel »
    await seed(ojja)
    a = await ok(client, qa, MATCH, {"have": [41, 1, 77]})
    # QA 22/10 r3-B1: never sent as « j'ai déjà » either — it would hide the persil another recipe must buy
    assert groups(a) == {0: ["ojja-merguez"]} and 17 not in card(a, "ojja-merguez")["at_home"]


async def test_the_placard_is_there_unless_taken_out_and_alone_it_is_not_enough(client, qa, ods_prices):  # noqa: F811
    await seed(SALADE)
    assert groups(await ok(client, qa, MATCH, {"have": [1, 3]})) == {0: ["salade-tomate"]}
    off = await ok(client, qa, MATCH, {"have": [1, 3], "pantry_off": [116]})
    assert groups(off) == {1: ["salade-tomate"]}
    assert card(off, "salade-tomate")["missing"][0]["name_fr"] == "Huile d'olive"
    no_placard = await ok(client, qa, MATCH, {"have": [1, 3], "use_pantry": False})
    assert groups(no_placard) == {1: ["salade-tomate"]}
    oil_alone = await ok(client, qa, MATCH, {"have": [116]})
    assert oil_alone["total"] == 0  # the placard alone is not « avec ce que j'ai »


async def test_darija_arabic_and_synonym_words_are_understood(client, qa, ods_prices):  # noqa: F811
    await seed(SALADE)
    a = await ok(client, qa, MATCH, {"have": ["tmatem", "بصل", "rien du tout"]})
    assert a["have"] == [1, 3] and a["unknown"] == ["rien du tout"]
    assert groups(a) == {0: ["salade-tomate"]}
    b = await ok(client, qa, MATCH, {"have": ["Tomates fraîches", "BSAL"]})
    assert b["have"] == [1, 3]
    d = await ok(client, qa, "listPantryIngredients")
    tomate = next(i for i in d["ingredients"] if i["id"] == 1)
    assert tomate["darija"] == ["tmatem"] and tomate["uses"] == 1 and 116 in d["placard"]
    assert d["quick"][:3] == [77, 1, 3]  # oeufs, tomate, oignon


async def test_the_list_filters_apply_and_the_order_is_mine_first_then_quickest(client, qa, ods_prices):  # noqa: F811
    await seed(OJJA, SALADE)
    a = await ok(client, qa, MATCH, {"have": [1, 3, 41, 77, 17]})
    assert groups(a) == {0: ["ojja-merguez", "salade-tomate"]}  # 4 of his ingredients before 2
    assert groups(await ok(client, qa, MATCH, {"have": [1, 3, 41, 77, 17], "max_time": 20})) == {
        0: ["salade-tomate"]
    }


async def test_admins_only(client, factory, ods_prices):  # noqa: F811
    await seed(SALADE)
    for user in (await factory.user(), await factory.user(role="courier")):
        for name in (MATCH, "listPantryIngredients"):
            assert (await fn(client, user, name, {"have": [1]})).status_code == 403


def test_dictionary_resolves_plurals_and_counts_a_family_as_one():
    rows = [
        Ingredient(id=50, key="poulet entier", name_fr="Poulet entier", name_ar="دجاجة كاملة", pantry=False),
        Ingredient(
            id=51, key="cuisses de poulet", name_fr="Cuisses de poulet", name_ar="أفخاذ الدجاج", pantry=False
        ),
        Ingredient(id=3, key="oignon", name_fr="Oignon", name_ar="بصل", pantry=False),
        Ingredient(id=5, key="poivron vert", name_fr="Poivron vert", name_ar="فلفل أخضر", pantry=False),
    ]
    d = rp.Dictionary.build(rows)
    assert d.resolve("oignons") == 3 and d.resolve("3") == 3 and d.resolve(999) is None
    assert d.resolve("poulet") == 50  # synonym of the referential file
    assert d.resolve("felfel akhdher morabba") == 5 and d.resolve("فلفل أخضر") == 5
    assert d.widen({50}) == {50, 51}
