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
    # a real draw since 22/10 (the seed weighs): the shared parsley wins most weeks, not every week
    wins = sum(
        rw.plan_week(pool, opts(3, seed=s), fixed={0: pool[0]}).days[1].id == "shares-parsley"
        for s in range(60)
    )
    assert 36 <= wins < 60


def test_quick_from_monday_to_thursday_longer_dishes_on_friday_and_the_weekend():
    cats = ["plat en sauce", "ojja", "riz", "pâtes", "légumineuses", "gratin"]
    pool = [cand(f"q{i}", f"c{i}", cats[i], rw.PROTEINS[i % 5], time=25) for i in range(6)]
    pool += [cand("slow", "tunisienne", "couscous", "poultry", time=120, tags=rw._folded("Week-end"))]
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


# --- QA 22/10 (r4 « Ma semaine », owner's list 8–12) -------------------------------------------------------


def test_semoule_is_not_fish_and_proteins_are_read_from_every_ingredient():
    """#8: « moule » is not in « semoule »; #12: minced meat in an « eggs » dish is red meat, chicken in an
    ojja is poultry; legumes / cheese make a meatless evening a meal."""
    assert rw.protein_of([("semoule fine semoule fine", 0.5), ("carotte", 0.3)]) == ("veg", False, False)
    assert rw.protein_of([("moules", 1.0, "epicerie")]) == ("fish", False, True)
    assert rw.protein_of([("cornichon", 0.1)])[2] is False  # « thon » only at the start of a word
    makhlama = rw.proteins_in([("oeufs", 0.48), ("viande hachee", 0.25), ("tomate", 0.4)])
    assert makhlama["main"] == "eggs" and makhlama["meats"] == {"red"}
    ojja = rw.proteins_in([("oeufs", 0.36), ("escalope de poulet escalope", 0.3)])
    assert "poultry" in ojja["meats"]
    assert rw.proteins_in([("bouillon de boeuf", 0.01), ("pois chiches", 0.4)]) == {
        "main": "veg", "meats": frozenset(), "plant": True,
    }  # fmt: skip


def test_no_breakfast_side_dish_drink_or_starter_as_a_dinner():
    """#9: excluded by the tags and the name, not only the category."""
    o = opts(3)
    bad = [
        cand("galayet", "jordanienne", "légumes", "veg", tags=rw._folded("Petit-déjeuner", "Rapide")),
        cand("riz-vermicelles", "libanaise", "riz", "veg", tags=rw._folded("Quotidien", "Accompagnement")),
        rw.Cand(id="hummus", slug="hummus", title="Hummus el sham (boisson chaude de pois chiches)",
                cuisine="égyptienne", category="soupe", time_min=100, protein="veg", high=3, low=2),
        rw.Cand(id="pilaf", slug="riz-pilaf", title="Riz pilaf", cuisine="française", category="riz",
                time_min=25, protein="veg", high=3, low=2),
        cand("mbatan", "tunisienne", "légumes farcis", "red", tags=rw._folded("Ramadan", "Entrée")),
    ]  # fmt: skip
    assert not [c.id for c in bad if rw.evening_ok(c, o)]
    assert rw.evening_ok(bad[-1], rw.Options(nights=3, season="hiver", occasion="ramadan"))
    assert rw.evening_ok(cand("tajine", tags=rw._folded("Ramadan", "Fête", "Entrée")), o)


def test_every_evening_has_a_protein_and_red_meat_poultry_are_counted_hidden():
    """#10/#12: a vegetable soup only when nothing else fits; minced meat in eggs counts for the one red meat;
    two evenings side by side never share a meat (ojja au poulet then poulet rôti)."""
    pool = [
        cand("makhlama", "irakienne", "œufs", "eggs", meats=frozenset({"red"}), time=30),
        cand("ojja-merguez", "tunisienne", "ojja", "red", meats=frozenset({"red"}), time=30),
        cand("ojja-poulet", "libyenne", "plat en sauce", "eggs", meats=frozenset({"poultry"}), time=30),
        cand("poulet-roti", "française", "volaille", "poultry", meats=frozenset({"poultry"}), time=30),
        cand("veloute", "française", "soupe", "veg", has_protein=False, time=30, high=2),
        cand("lentilles", "marocaine", "légumineuses", "veg", time=30),
        cand("sardines", "algérienne", "poisson", "fish", meats=frozenset({"fish"}), time=30),
    ]
    for k in range(20):
        p = rw.plan_week(pool, opts(3, seed=k))
        ids = [c.id for c in p.days]
        assert p.ok and "veloute" not in ids
        assert not {"makhlama", "ojja-merguez"} <= set(ids)
        assert all(not (x.meats & y.meats) for x, y in zip(p.days, p.days[1:], strict=False))


def test_change_keeps_45_min_on_a_weekday_even_when_the_wishes_are_loosened():
    """#12 (bug 4): « Changer » on Wednesday with only Tunisian dishes left: a quick one, never the 60 min."""
    kept = {
        0: cand("mon", "tunisienne", "ojja", "eggs", time=30),
        2: cand("wed", "tunisienne", "pâtes", "fish", time=30),
    }
    pool = [*kept.values(), cand("marqa", "tunisienne", "plat en sauce", "veg", time=60, high=3),
            cand("quick", "tunisienne", "volaille", "poultry", time=30, high=12),
            cand("old", "tunisienne", "riz", "red", time=25)]  # fmt: skip
    for k in range(10):
        p = rw.plan_week(pool, opts(3, seed=k), fixed=kept, avoid={"old"})
        assert p.ok and p.days[1].id == "quick"


def test_couscous_on_friday_for_a_tunisian_family_and_the_dear_fish_avoided_on_a_tight_budget():
    """#10: the couscous waits for Friday; a 34 DT merlan in an 80 DT week is not proposed."""
    cats = ["plat en sauce", "ojja", "riz", "pâtes", "légumineuses", "gratin", "tajine"]
    pool = [cand(f"q{i}", f"c{i}", cats[i], rw.PROTEINS[i % 5], time=30, high=10) for i in range(7)]
    pool += [cand("couscous", "tunisienne", "couscous", "poultry", time=105, high=15)]
    pool += [cand("merlan", "française", "poisson", "fish", time=20, high=34)]
    for k in range(15):
        p = rw.plan_week(pool, opts(5, budget=80, seed=k, couscous_friday=True))
        assert p.ok and p.days[4].id == "couscous" and "merlan" not in [c.id for c in p.days]


def test_the_seed_and_the_dishes_already_seen_give_another_week():
    """#10/#11: two seeds, two weeks; « Une autre semaine » with the dishes seen never comes back."""
    cats = ["plat en sauce", "ojja", "riz", "pâtes", "légumineuses", "gratin", "tajine", "volaille", "soupe"]
    pool = [
        cand(f"d{i}{j}", f"c{i}{j}", cats[i], rw.PROTEINS[(i + j) % 5], time=30)
        for i in range(9)
        for j in range(3)
    ]
    weeks = {tuple(c.id for c in rw.plan_week(pool, opts(4, seed=s)).days) for s in range(10)}
    assert len(weeks) >= 8
    seen: set[str] = set()
    shown = []
    prev: set[str] = set()
    for k in range(5):
        p = rw.plan_week(pool, opts(4, seed=k, seen=frozenset(seen)), avoid=prev)
        week = tuple(c.id for c in p.days)
        assert p.ok and week not in shown and not set(week) & seen
        shown.append(week)
        prev = set(week)
        seen |= prev


# --- re-check 22/10 (v2-semaine-placard V1–V4) ----------------------------------------------------------


QUICK = [
    ("ojja-escalope", "tunisienne", "ojja", "eggs", {"poultry"}, 12),
    ("makrouna-thon", "tunisienne", "pâtes", "fish", {"fish"}, 18),
    ("escalope-panee", "tunisienne", "volaille", "poultry", {"poultry"}, 21),
    ("ojja-crevettes", "tunisienne", "ojja", "fish", {"fish"}, 27),
    ("spaghetti-crevettes", "tunisienne", "pâtes", "fish", {"fish"}, 25),
    ("ojja-merguez", "tunisienne", "ojja", "red", {"red"}, 30),
    ("brochettes", "marocaine", "grillade", "red", {"red"}, 30),
    ("chakchouka-oeuf", "tunisienne", "œufs", "eggs", set(), 8),
    ("lablabi-rapide", "tunisienne", "légumineuses", "veg", set(), 7),
    ("omelette", "française", "œufs", "eggs", set(), 5),
    ("coquillettes", "française", "pâtes", "veg", set(), 9),
    ("flammekueche", "française", "tarte salée", "poultry", {"poultry"}, 14),
    ("poulet-creme", "française", "volaille", "poultry", {"poultry"}, 20),
    ("chich-taouk", "libanaise", "grillade", "poultry", {"poultry"}, 22),
    ("tajine-jben", "tunisienne", "tajine", "eggs", set(), 14),
    ("kefta-sauce-rapide", "algérienne", "plat en sauce", "red", {"red"}, 24),
]


def _family_pool():
    """A realistic pool: few quick dishes, several couscous, long stews; French dishes cheaper (omelette)."""
    pool = [cand(s, cu, cat, p, high=h, time=30, meats=frozenset(m)) for s, cu, cat, p, m, h in QUICK]
    for i, p in enumerate(["poultry", "fish", "red", "veg", "poultry", "veg"]):
        cu = "tunisienne" if i < 4 else "marocaine"
        pool.append(cand(f"couscous-{i}", cu, "couscous", p, high=15 + i, time=100))
    pool += [cand(f"stew-{i}", "tunisienne", "plat en sauce", "veg", high=10, time=90) for i in range(6)]
    pool += [cand("gratin-fr", "française", "gratin", "eggs", high=9, time=60)]
    return pool


def _family_week(phone, day, pool):
    """As the route: the phone's seed + the ISO week, the two weeks before weigh less, never the same week."""
    o = rw.Options(nights=5, budget=80, season="automne", couscous_friday=True, home_cuisine=True,
                   seed=rw.week_seed(phone, day))  # fmt: skip
    recent = rw.recent_weeks(pool, o, phone, day)
    o.recent = rw._recent_dishes(recent)
    return rw._draw_week(pool, o, recent)


def test_v1_every_family_and_every_week_gets_its_own_week_tunisian_dishes_first():
    """V1: 15 families (the default settings) → ≥ 12 different weeks; the same phone 10 Mondays in a row →
    10 different weeks; a Tunisian family: Tunisian / Maghrebi dishes the majority, couscous on Friday."""
    import random
    from datetime import timedelta

    pool = _family_pool()
    phones = [random.Random(2210 + k).randint(1, 999999) for k in range(15)]
    weeks = [_family_week(p, date(2026, 10, 22), pool) for p in phones]
    assert all(w.ok for w in weeks)
    assert len({tuple(c.id for c in w.days) for w in weeks}) >= 12
    assert len({c.id for w in weeks for c in w.days}) >= 18
    mondays = [date(2026, 10, 26) + timedelta(weeks=k) for k in range(10)]
    same = [tuple(c.id for c in _family_week(phones[0], d, pool).days) for d in mondays]
    assert len(set(same)) == 10
    days = [c for w in weeks for c in w.days]
    assert sum(c.cuisine in rw.MAGHREB for c in days) > len(days) / 2
    assert all(w.days[4].category == "couscous" for w in weeks)
    # the same phone, the same week: the same proposal (a reload does not change it)
    again = _family_week(phones[0], date(2026, 10, 22), pool)
    assert [c.id for c in again.days] == [c.id for c in weeks[0].days]


def test_v2_an_optional_meat_is_not_the_evenings_protein():
    """V2 (the pure part; the route test below reads the optional lines): no line, no protein."""
    marqa = rw.proteins_in([("pomme de terre", 1.0), ("tomate", 0.3)])
    assert marqa["main"] == "veg" and not marqa["meats"] and not marqa["plant"]


def test_v3_change_on_a_weekday_never_proposes_a_long_dish():
    """V3: Wednesday, the neighbours eat chicken: a quick chicken dish before a 50-min fish; never the 3 h
    madfouna; nothing quick left → no plan (the route says « no_other » with quick_day)."""
    kept = {
        0: cand("mon", "tunisienne", "ojja", "eggs", time=30),
        1: cand("tue", "tunisienne", "volaille", "poultry", time=30, meats=frozenset({"poultry"})),
        3: cand("thu", "française", "tarte salée", "poultry", time=30, meats=frozenset({"poultry"})),
        4: cand("fri", "tunisienne", "couscous", "fish", time=100),
    }
    quick = cand("poulet-rapide", "tunisienne", "grillade", "poultry", time=35, meats=frozenset({"poultry"}))
    fish50 = cand("ejja", "libyenne", "poisson", "fish", time=50, meats=frozenset({"fish"}))
    madfouna = cand("madfouna", "tunisienne", "plat en sauce", "red", time=180, meats=frozenset({"red"}))
    pool = [*kept.values(), quick, fish50, madfouna]
    for k in range(10):
        p = rw.plan_week(pool, opts(5, seed=k), fixed=kept, avoid={"old"})
        assert p.ok and p.days[2].id == "poulet-rapide"
    p = rw.plan_week(pool, opts(5, seed=1), fixed=kept, avoid={"poulet-rapide"})
    assert p.ok and p.days[2].id == "ejja"  # 60 min at most, the last resort
    p = rw.plan_week(pool, opts(5, seed=1), fixed=kept, avoid={"poulet-rapide", "ejja"})
    assert not p.ok  # never the 3 h madfouna on a Wednesday


def test_v4_a_dish_above_one_order_is_never_proposed_even_with_a_free_budget():
    """V4: couscous au poisson 86.5 DT with « Libre »: one order is at most 80 DT, so never in the week."""
    cats = ["plat en sauce", "ojja", "riz", "pâtes", "légumineuses", "gratin"]
    pool = [cand(f"q{i}", f"c{i}", cats[i], rw.PROTEINS[i % 5], time=30, high=20) for i in range(6)]
    pool += [cand("couscous-poisson", "tunisienne", "couscous", "fish", time=90, high=86.5)]
    for k in range(10):
        p = rw.plan_week(pool, opts(5, seed=k, couscous_friday=True))
        assert p.ok and "couscous-poisson" not in [c.id for c in p.days]
        assert all(c.high <= rw.BASKET_CAP for c in p.days)


def _with_optional(d, *lines):
    return {**d, "ingredients": [*d["ingredients"], *[{**x, "optional": True} for x in lines]]}


OPTIONAL_DISHES = [
    _with_optional(
        dish("marqa-batata", "tunisienne", "plat en sauce", 40, [(1, 0.5, "kg"), (3, 0.3, "kg")]),
        {"ingredient_id": 50, "qty": 1, "unit": "kg"},
    ),
    _with_optional(
        dish("kafteji-test", "tunisienne", "légumes", 40, [(77, 6, "piece"), (1, 0.5, "kg")]),
        {"ingredient_id": 41, "qty": 0.5, "unit": "kg"},
    ),
]


async def test_v2_the_optional_chicken_or_merguez_is_not_counted_by_the_route(client, qa, week_prices):  # noqa: F811
    """V2: marqa batata (optional chicken) is a meatless evening, never proposed while a dish with a protein
    fits; kafteji (optional merguez) is an eggs evening and stays with « Sans viande rouge »."""
    from app.db import transaction
    from app.services import recipes_import

    await seed_week()
    async with transaction() as session:
        await recipes_import.import_recipes(session, OPTIONAL_DISHES)
    listed = await ok(client, qa, "listRecipes", {"page_size": 40})
    ids = {r["slug"]: r["id"] for r in listed["recipes"]}
    kept = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN, "no_red_meat": True,
                                       "fixed": [{"day": 0, "recipe_id": ids["kafteji-test"]},
                                                 {"day": 1, "recipe_id": ids["marqa-batata"]}]})  # fmt: skip
    assert kept["ok"] and [d["protein"] for d in kept["days"][:2]] == ["eggs", "veg"]
    assert slugs(kept)[:2] == ["kafteji-test", "marqa-batata"]  # « Sans viande rouge » keeps the kafteji
    for seed_ in range(4):
        a = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN, "seed": seed_})
        assert a["ok"] and "marqa-batata" not in slugs(a)


async def test_v3_change_says_no_other_quick_dish_on_a_weekday(client, qa, week_prices):  # noqa: F811
    await seed_week()
    a = await ok(client, qa, PLAN, {"people": 4, "nights": 3, "today": AUTUMN})
    listed = await ok(client, qa, "listRecipes", {"page_size": 40})
    every = [r["id"] for r in listed["recipes"]]
    fixed = [{"day": d, "recipe_id": a["days"][d]["card"]["id"]} for d in (0, 2)]
    body = {"people": 4, "nights": 3, "today": AUTUMN, "fixed": fixed, "avoid": every}
    none = await ok(client, qa, PLAN, body)
    assert none["ok"] is False and none["reason"] == "no_other" and none["quick_day"] is True
