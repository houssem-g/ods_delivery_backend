"""« Ma semaine » (owner 21/10, first version): one dinner per evening chosen by rules, no AI
(app/services/recipes_week.py). One short test per rule: the budget, the variety, the shared articles, the
days, the moment, « Changer », impossible → the smallest budget, the 80 DT baskets, admins only."""

from datetime import date

import httpx
import pytest

from app.config import settings
from app.services import recipe_prices
from app.services import recipes_week as rw
from tests.test_recipes import qa, seed  # noqa: F401  (fixtures)
from tests.vitrine_helpers import fn, ok

PLAN = "planRecipeWeek"
AUTUMN = "2026-10-21"


# --- pure: the greedy choice ---------------------------------------------------------------------------


def cand(cid, cuisine="tunisienne", category="plat en sauce", protein="poultry", high=10.0, time=30, **kw):
    return rw.Cand(id=cid, slug=cid, title=cid, cuisine=cuisine, category=category, time_min=time,
                   protein=protein, high=high, low=high - 1, **kw)  # fmt: skip


def opts(nights=5, **kw):
    return rw.Options(nights=nights, season="automne", **kw)


def test_neighbour_evenings_never_share_cuisine_category_or_protein_and_one_red_meat_at_most():
    pool = [
        cand("a", "tunisienne", "plat en sauce", "poultry"),
        cand("b", "tunisienne", "ojja", "red"),
        cand("c", "marocaine", "tajine", "red"),
        cand("d", "libanaise", "légumineuses", "veg"),
        cand("e", "tunisienne", "poisson", "fish"),
        cand("f", "française", "pâtes", "eggs"),
        cand("g", "marocaine", "couscous", "poultry"),
    ]
    p = rw.plan_week(pool, opts(5))
    assert p.ok and len({c.id for c in p.days}) == 5
    for x, y in zip(p.days, p.days[1:], strict=False):
        assert x.cuisine != y.cuisine and x.category != y.category and x.protein != y.protein
    assert sum(1 for c in p.days if c.protein == "red") == 1
    assert p.loosened == []


def test_one_cuisine_asked_loosens_the_cuisine_rule_only_and_says_so():
    pool = [cand(str(i), "tunisienne", cat, prot) for i, (cat, prot) in enumerate(
        [("plat en sauce", "poultry"), ("poisson", "fish"), ("légumineuses", "veg"), ("ojja", "eggs"),
         ("couscous", "poultry")])]  # fmt: skip
    p = rw.plan_week(pool, opts(4))
    assert p.ok and p.loosened == ["cuisine"]
    assert all(x.protein != y.protein for x, y in zip(p.days, p.days[1:], strict=False))


def test_an_article_already_bought_another_evening_wins_less_waste():
    persil = 17
    p_ = frozenset({persil})
    pool = [
        cand("monday", "tunisienne", "plat en sauce", "poultry", high=10, fresh=p_, buys=p_),
        cand("same-price-no-share", "marocaine", "légumineuses", "veg", high=10, buys=frozenset({99})),
        cand("shares-parsley", "libanaise", "légumineuses", "veg", high=10, fresh=p_, buys=p_),
    ]
    p = rw.plan_week(pool, opts(3, seed=1), fixed={0: pool[0]})
    assert p.days[1].id == "shares-parsley"


def test_quick_from_monday_to_thursday_longer_dishes_on_friday_and_the_weekend():
    cats = ["plat en sauce", "ojja", "riz", "pâtes", "légumineuses", "gratin"]
    pool = [cand(f"q{i}", f"c{i}", cats[i], rw.PROTEINS[i % 5], time=25) for i in range(6)]
    pool += [cand("slow", "tunisienne", "couscous", "poultry", time=120, tags=frozenset({"week-end"}))]
    pool += [cand("long-weekday", "x", "tajine", "red", time=90)]
    p = rw.plan_week(pool, rw.Options(nights=7, season="automne"))
    assert p.ok
    assert all(c.time_min <= rw.QUICK_MIN for c in p.days[:4])
    assert "slow" in [c.id for c in p.days[4:]]


def test_ramadan_puts_the_chorba_and_the_brik_first_the_brik_is_never_an_evening_otherwise():
    soup = cand(
        "chorba",
        "tunisienne",
        "soupe",
        "red",
        high=9,
        tags=frozenset({"ramadan"}),
        seasons=frozenset({"ramadan"}),
    )
    brik = cand("brik", "tunisienne", "brik", "eggs", high=6)
    pool = [
        soup,
        brik,
        cand("p", "marocaine", "plat en sauce", "poultry", high=8),
        cand("l", "libanaise", "légumineuses", "veg", high=8),
    ]
    ramadan = rw.plan_week(pool, rw.Options(nights=3, season="hiver", occasion="ramadan"))
    assert {"chorba", "brik"} <= {c.id for c in ramadan.days}
    autumn = rw.plan_week(pool, rw.Options(nights=3, season="automne"))
    assert "brik" not in {c.id for c in autumn.days}
    assert rw.moment_of(date(2027, 2, 20)) == {"season": "hiver", "occasion": "ramadan"}
    assert rw.moment_of(date(2026, 10, 21)) == {"season": "automne", "occasion": None}


def test_the_budget_is_kept_for_the_cheapest_remaining_evenings_and_impossible_says_the_least():
    cats = ["plat en sauce", "ojja", "tajine", "riz", "pâtes", "gratin"]
    pool = [
        cand(f"r{i}", f"c{i}", cats[i], rw.PROTEINS[i % 5], high=h)
        for i, h in enumerate([30, 25, 9, 8, 7, 6])
    ]
    p = rw.plan_week(pool, opts(3, budget=40))
    assert p.ok and sum(c.high for c in p.days) <= 40
    assert not rw.plan_week(pool, opts(3, budget=15)).ok
    least = rw.min_budget(pool, opts(3, budget=15))
    assert least == 25.0  # 6 + 7 + 8 = 21 → 25 DT
    assert rw.plan_week(pool, opts(3, budget=least)).ok


def test_protein_of_reads_the_heaviest_line_and_merguez_is_red_meat():
    assert rw.protein_of([("merguez", 0.5), ("oeufs", 0.36), ("tomate", 0.6)]) == ("red", True, False)
    assert rw.protein_of([("cuisses de poulet", 1.0), ("thon", 0.1)]) == ("poultry", False, True)
    assert rw.protein_of([("lentilles", 0.5), ("oignon", 0.2)]) == ("veg", False, False)
    assert rw.protein_of([("viande de boeuf", 0.5)])[0] == "red"  # « boeuf » is not « oeuf »
    assert rw.protein_of([("daurade", 1.0, "poissonnerie"), ("oeufs", 0.12, "cremerie")]) == (
        "fish",
        False,
        True,
    )


# --- the route (real database, prices from a mocked ODS) ------------------------------------------------


def ref(iid, key, fr, ar, unit, aisle, pantry=False, piece_kg=None):
    row = {"id": iid, "key": key, "fr": fr, "ar": ar, "unit": unit, "aisle": aisle, "pantry": str(pantry)}
    return {**row, "piece_kg": str(piece_kg)} if piece_kg else row


INGREDIENTS = [
    ref(1, "tomate", "Tomate", "طماطم", "kg", "marche"),
    ref(3, "oignon", "Oignon", "بصل", "kg", "marche"),
    ref(17, "persil", "Persil", "معدنوس", "botte", "marche", piece_kg=0.1),
    ref(41, "merguez", "Merguez", "مرقاز", "kg", "boucherie"),
    ref(50, "poulet entier", "Poulet entier", "دجاجة كاملة", "kg", "boucherie"),
    ref(65, "sardine", "Sardine", "سردينة", "kg", "poissonnerie"),
    ref(77, "oeufs", "Oeufs", "بيض", "piece", "cremerie", piece_kg=0.06),
    ref(90, "lentilles", "Lentilles", "عدس", "kg", "epicerie"),
    ref(91, "semoule", "Semoule", "سميد", "kg", "epicerie"),
    ref(116, "huile d'olive", "Huile d'olive", "زيت زيتون", "l", "epicerie", pantry=True),
]


def price(lo, hi, unit="kg"):
    return {"min": lo, "max": hi, "unit": unit, "method": "retail", "source": "Carrefour Tunisie (en ligne)",
            "as_of": "2026-10-21", "stale": False, "to_check": False}  # fmt: skip


PRICES = {
    1: price(1.85, 2.3),
    3: price(1.725, 1.725),
    17: price(0.8, 1.0, "piece"),
    41: price(45.2, 46.8),
    50: price(10, 11),
    65: price(6, 7),
    77: price(0.44, 0.48, "piece"),
    90: price(4.5, 5),
    91: price(1.5, 1.8),
}
ODS = {
    "country": "TN",
    "rules": {"min_priced_share": 0.7, "stale_days": 14, "rounding_tnd": 0.5},
    "ingredients": [
        {"id": r["id"], "key": r["key"], "name_fr": r["fr"], "name_ar": r["ar"], "aisle": r["aisle"],
         "buy_unit": r["unit"], "pantry": False, "price": PRICES[r["id"]]}
        for r in INGREDIENTS if r["id"] in PRICES
    ],
}  # fmt: skip


@pytest.fixture
def week_prices(monkeypatch):
    monkeypatch.setattr(settings, "ODS_API_BASE_URL", "http://ods.test")
    monkeypatch.setattr(settings, "ODS_RECIPES_SECRET", "s" * 40)
    recipe_prices.set_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=ODS)))
    yield
    recipe_prices.set_transport(None)


def dish(slug, cuisine, category, time, lines, **kw):
    return {"slug": slug, "title_fr": slug.replace("-", " ").title(), "title_ar": slug, "cuisine": cuisine,
            "category": category, "time_min": time, "servings": 4,
            "ingredients": [{"ingredient_id": i, "qty": q, "unit": u} for i, q, u in lines],
            **kw}  # fmt: skip


DISHES = [
    dish("ojja-merguez", "tunisienne", "ojja", 25,
         [(41, 0.5, "kg"), (1, 0.6, "kg"), (77, 6, "piece"), (17, 1, "botte")]),
    dish("poulet-sauce", "tunisienne", "plat en sauce", 40,
         [(50, 1, "kg"), (1, 0.5, "kg"), (3, 0.2, "kg"), (17, 1, "botte")]),
    dish("sardines-four", "tunisienne", "poisson", 35, [(65, 0.8, "kg"), (1, 0.3, "kg"), (17, 1, "botte")]),
    dish("chakchouka", "tunisienne", "œufs", 20, [(77, 6, "piece"), (1, 0.5, "kg"), (3, 0.2, "kg")]),
    dish("lentilles", "marocaine", "légumineuses", 40, [(90, 0.5, "kg"), (3, 0.2, "kg"), (1, 0.2, "kg")]),
    dish("couscous-poulet", "tunisienne", "couscous", 90, [(50, 1, "kg"), (91, 1, "kg"), (3, 0.2, "kg")],
         tags=["Week-end"]),
    dish("salade-ete", "tunisienne", "salade", 10, [(1, 0.5, "kg"), (116, 2, "cas")]),
    dish("makroudh", "tunisienne", "douceur", 60, [(91, 1, "kg")]),
]  # fmt: skip


async def seed_week():
    from app.db import transaction
    from app.services import recipes_import

    async with transaction() as session:
        await recipes_import.import_ingredients(session, INGREDIENTS)
        await recipes_import.import_recipes(session, DISHES)


def slugs(answer):
    return [d["card"]["slug"] for d in answer["days"]]


async def test_a_week_within_its_budget_with_one_line_per_evening_the_merged_total_and_shared_articles(
    client,
    qa,  # noqa: F811
    week_prices,
):
    await seed_week()
    a = await ok(client, qa, PLAN, {"people": 4, "nights": 5, "budget": 120, "today": AUTUMN})
    assert a["ok"] and len(a["days"]) == 5 and len(set(slugs(a))) == 5
    assert "salade-ete" not in slugs(a) and "makroudh" not in slugs(a)  # no summer salad in autumn, no pastry
    assert a["total"]["high"] <= 120 and a["total"]["show_total"]
    assert a["days"][0]["card"]["cost_servings"] == 4 and a["days"][0]["card"]["cost"]["show_total"]
    assert all(d["day"] == i for i, d in enumerate(a["days"]))
    shared = {s["name_fr"]: s["nights"] for s in a["shared"]}
    assert shared["Tomate"] >= 3 and a["savings"] >= 0
    assert a["separate_high"] >= a["total"]["high"]
    ojja_or_not = [d for d in a["days"] if d["card"]["slug"] == "ojja-merguez"]
    assert all(d["protein"] == "red" for d in ojja_or_not)
    assert sum(1 for d in a["days"] if d["protein"] == "red") <= 1


async def test_too_small_a_budget_is_said_with_the_smallest_budget_that_works(client, qa, week_prices):  # noqa: F811
    await seed_week()
    a = await ok(client, qa, PLAN, {"people": 4, "nights": 5, "budget": 20, "today": AUTUMN})
    assert a["ok"] is False and a["reason"] == "budget" and a["min_budget"] % 5 == 0 and a["min_budget"] > 20
    b = await ok(client, qa, PLAN, {"people": 4, "nights": 5, "budget": a["min_budget"], "today": AUTUMN})
    assert b["ok"] and b["total"]["high"] <= a["min_budget"]
    few = await ok(client, qa, PLAN, {"people": 4, "nights": 7, "cuisines": ["marocaine"], "today": AUTUMN})
    assert few["ok"] is False and few["reason"] == "few"


async def test_change_one_evening_keeps_the_others_and_says_when_nothing_else_fits(client, qa, week_prices):  # noqa: F811
    await seed_week()
    a = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN})
    ids = [d["card"]["id"] for d in a["days"]]
    fixed = [{"day": 0, "recipe_id": ids[0]}, {"day": 2, "recipe_id": ids[2]}]
    b = await ok(
        client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN, "fixed": fixed, "avoid": [ids[1]]}
    )
    assert b["ok"] and [d["card"]["id"] for d in b["days"]][0::2] == [ids[0], ids[2]]
    assert b["days"][1]["card"]["id"] != ids[1]
    all_ids = [c["card"]["id"] for c in a["days"]] + [b["days"][1]["card"]["id"]]
    seen = await ok(client, qa, "listRecipes", {"page_size": 40})
    every = [r["id"] for r in seen["recipes"]]
    none = await ok(
        client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN, "fixed": fixed, "avoid": every}
    )
    assert none["ok"] is False and none["reason"] == "no_other" and all_ids


async def test_wishes_no_red_meat_no_fish_quick_and_what_i_have(client, qa, week_prices):  # noqa: F811
    await seed_week()
    wishes = {"no_red_meat": True, "no_fish": True, "quick": True}
    a = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN, **wishes})
    assert a["ok"] and not {"ojja-merguez", "sardines-four", "couscous-poulet"} & set(slugs(a))
    assert all(d["card"]["time_min"] <= 45 for d in a["days"])
    have = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "have": [90, 77], "today": AUTUMN})
    assert {"lentilles", "chakchouka"} <= set(slugs(have)) and {90, 77} <= set(have["at_home"])


async def test_a_week_above_80_dt_is_cut_into_orders_of_80_dt_at_most(client, qa, week_prices):  # noqa: F811
    await seed_week()
    a = await ok(client, qa, PLAN, {"people": 8, "nights": 5, "today": AUTUMN})
    assert a["ok"] and a["total"]["high"] > 80 and a["basket_cap"] == 80
    assert len(a["baskets"]) >= 2 and sum(len(b["days"]) for b in a["baskets"]) == 5
    assert all(b["high"] <= 80 or len(b["days"]) == 1 for b in a["baskets"])


async def test_admins_only_and_a_wrong_budget_is_refused(client, factory, qa, week_prices):  # noqa: F811
    await seed_week()
    for user in (await factory.user(), await factory.user(role="courier")):
        assert (await fn(client, user, PLAN, {"people": 4, "nights": 5})).status_code == 403
    assert (await fn(client, qa, PLAN, {"people": 4, "nights": 5, "budget": -3})).status_code == 400
    libre = await ok(client, qa, PLAN, {"people": 4, "nights": 9, "budget": None, "today": AUTUMN})
    assert libre["nights"] == 7 and libre["budget"] is None  # 9 evenings → 7 (and 6 dinners: too few)
    assert libre["ok"] is False and libre["reason"] == "few"
