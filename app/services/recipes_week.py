"""« Ma semaine » (Recettes, owner 21/10: « j'accepte tout, exécute » — in the FIRST version): the customer
says how many people, how many evenings (3 to 7), a budget for the week (or none) and a few wishes; the
server proposes one dinner per evening by RULES (a greedy choice, no AI), each evening replaceable
(« Changer ») or kept (the padlock), then « Tout mettre dans Ma liste ».

Rules (one place, tested in tests/test_recipes_week.py):
- Only recipes whose cost is KNOWN for N people (a total shown: ≥ 70 % of the articles and of the weight
  priced, recipe_prices.cost), dishes counted in people, and an evening category (a stew, a couscous, fish,
  pasta, eggs, a soup — soups and briks first in Ramadan, salads only in summer; never a pastry, a drink,
  a sauce).
- Never a breakfast, a side dish, a drink, a mezzé (the tags and the name, not only the category); a starter
  only in Ramadan (QA 22/10).
- Variety: never the same dish twice; two evenings side by side never share the cuisine, the category nor the
  protein (main, or any meat / fish the dish contains: minced meat in an « eggs » dish is red meat); at most
  one red meat (two from 6 evenings) and two poultry (three from 6), counted from the ingredients; every
  evening has a protein (meat, fish, eggs, legumes, cheese). A real draw (the phone's seed + the week of the
  year), the dishes already seen this session last. When the wishes leave too few dishes, the rules are
  loosened in this order: cuisine, protein, category (see STEPS).
- A Tunisian family (no cuisine wish, or « Tunisienne »): the couscous on Friday, the everyday Tunisian dishes
  (STAPLES) preferred; with a budget, one evening never above twice its part (a 34 DT merlan of 80).
- Monday to Thursday: ready in 45 min or less, kept while the cuisine wish and the protein rules are loosened
  (« Changer » too); 60 only as the last resort, NEVER more (QA 22/10 V3); Friday and the weekend: the
  longer, festive dishes (couscous, « Week-end » / « Fête » / « Repas du vendredi »), never a 30-min snack.
- Never a dish above BASKET_CAP alone (it could not be ordered, QA 22/10 V4). Optional lines (« facultatif »)
  are not bought, so they are never the evening's protein nor a meat for the limits (QA 22/10 V2).
- The draw (QA 22/10 V1): the near-best dishes drawn at random (seed of the phone + account + ISO week), a
  Tunisian family (no cuisine wish) keeps Tunisian / Maghrebi dishes the majority.
- Less waste: an article already bought for another evening (a bunch of parsley, tomatoes, onions) is worth
  points — the fresh ones of the market most; « avec ce que j'ai » (the phone's list): the dishes using it
  too.
- The moment: Ramadan (chorba, briks, the « Ramadan » dishes), the season of the files (« saison »), Aïd
  el-Kébir (the sheep's meat). Same calendar as the home row (front lib/recipesHome.js), approximate dates.
- Budget: the week's total (its high end) stays ≤ the budget; each evening keeps enough for the cheapest
  possible remaining evenings. Impossible → said clearly, with the smallest budget that works (rounded up to
  5 DT), never a plan above the budget.
- The week's cost is the MERGED list (one bunch of parsley for three dishes): `savings` = the sum of the
  dishes bought one by one minus the merged list. `baskets`: the evenings cut into orders of at most 80 DT
  each (the basket ceiling of the users' council, UX §2 B2) — the order rules themselves are unchanged.

Visibility: like every recipe route, ADMINS ONLY for now (recipes._require_see). The pool is what the admins
browse (recipes.BROWSABLE). No migration.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Recipe
from app.security.deps import CurrentUser
from app.services import recipe_prices, recipes
from app.services import recipe_units as ru

PEOPLE_MIN, PEOPLE_MAX = 1, 12
NIGHTS_MIN, NIGHTS_MAX = 3, 7
BUDGET_MIN, BUDGET_MAX = 5, 1000
BASKET_CAP = 80  # DT per order (users' council, UX §2 B2)
QUICK_MIN = 45  # Monday → Thursday
LONG_DAYS = (4, 5, 6)  # Friday, Saturday, Sunday (0 = Monday)
MIN_BUDGET_STEP = 5
POOL_MAX = 800
SEEN_MAX = 300

# The evening categories (the files' « categorie »). Soups and briks: an evening in Ramadan (chorba + brik).
DINNER = frozenset(
    {
        "plat en sauce", "soupe", "poisson", "riz", "couscous", "volaille", "pâtes", "légumineuses", "tajine",
        "légumes farcis", "viande", "légumes", "tarte salée", "ojja", "œufs", "oeufs", "gratin",
        "plat au four", "plat mijoté", "plat de fête", "grillade", "végétarien", "plat sucré-salé",
        "viande rôtie", "plat", "brik", "salade",
    }
)  # fmt: skip
RAMADAN_ONLY = frozenset({"brik"})
SUMMER_ONLY = frozenset({"salade"})


def _folded(*words: str) -> frozenset[str]:
    """Tags are compared FOLDED (« Week-end » → « week end », « Fête » → « fete »), as `_words` keeps them."""
    return frozenset(recipes.fold(w) for w in words)


LONG_TAGS = _folded("Week-end", "Fête", "Invités", "Repas du vendredi", "Vendredi", "Repas de famille", "Aïd")
QUICK_TAGS = _folded("Rapide", "Quotidien")
# Not a family dinner whatever the file's category (owner 22/10): a breakfast (galayet bandora, makhlama), a
# side dish (riz aux vermicelles), a drink filed as a soup (hummus el sham), a mezzé, a sauce…
NOT_DINNER_TAGS = _folded(
    "Petit-déjeuner", "Accompagnement", "Boisson", "Goûter", "Dessert", "Sauce", "Thé", "Thé ou café",
    "Kemia", "Mezzé", "Amuse-bouche", "Apéritif", "Base", "Avec le poisson", "Après le repas",
)  # fmt: skip
# A starter / a sandwich is an evening only in Ramadan (chorba + brik + a starter), or when the file also
# calls it a meal (« Famille », « Quotidien », « Fête »…).
STARTER_TAGS = _folded("Entrée", "Sandwich", "Street food")
MEAL_TAGS = _folded(
    "Famille", "Repas de famille", "Quotidien", "Fête", "Week-end", "Invités", "Repas du vendredi", "Vendredi"
)
# …and by the name, when the tags say nothing (« Riz pilaf », « boisson chaude de pois chiches »).
NOT_DINNER_TITLES = (
    "boisson",
    "riz pilaf",
    "riz aux vermicelles",
    "riz sucre",
    "accompagnement",
    "garniture",
)
# The Tunisian everyday dishes a Tunisian family expects in its weeks (owner 22/10), by the slug's words.
STAPLES = (
    "mloukhia", "kamounia", "lablabi", "chorba", "marka", "marqa", "loubia", "kefta sauce", "jelbana",
    "hlalem", "rechta", "mderbel", "kafteji", "chakchouka", "mhamsa", "felfel mehchi", "makrouna", "nwasser",
    "dwida", "tajine malsouka", "kabkabou", "couscous",
)  # fmt: skip
JITTER = 1.0  # a tie-break only: the variety is the draw below (DRAW_*), not a jitter on the best score
# The draw (QA 22/10 V1): among the dishes that keep every rule at this step, those within DRAW_WINDOW points
# of the best are drawn at random, weighted exp((score - best) / DRAW_TEMP): the preferences (couscous on
# Friday, less waste, the cost) still weigh, but every family and every week gets its own week.
DRAW_WINDOW = 10.0
DRAW_TEMP = 5.0
# A Tunisian family (no cuisine wish): Tunisian / Maghrebi dishes are the majority of its week (QA 22/10 V1),
# at most nights // 3 evenings (1 of 5, 2 of 7) of another cuisine while the cuisine rule holds.
MAGHREB = frozenset({"tunisienne", "marocaine", "algérienne", "algerienne", "libyenne", "maghrébine"})
DEAR = 2.0  # with a budget, one evening never costs more than twice its part (budget / evenings)
SEEN_MALUS = 8.0  # a dish already shown this session (« Une autre semaine » never goes back)
RECENT_MALUS = 5.0  # a dish of last week's proposal (QA 22/10 V1: next Monday, another week)

# The proteins, from the dictionary keys of the lines (folded: no accent, no case), matched at the START OF A
# WORD (« moule » is not in « semoule », « oeuf » is not in « boeuf », « thon » in « thon » only).
RED = (
    "agneau", "boeuf", "bœuf", "veau", "mouton", "viande", "merguez", "kaddid", "tripes", "kefta", "osbane",
    "foie", "kebda", "abats", "pieds d agneau",
)  # fmt: skip
POULTRY = ("poulet", "dinde", "caille", "volaille", "pigeon")
FISH = ("thon", "anchois", "sardine", "poisson", "crevette", "calamar", "seiche", "poulpe", "moule", "morue")
EGGS = ("oeuf", "œuf")
# What makes a vegetarian evening a real meal: legumes, cheese (lablabi, marka, tajine jben).
PLANT_PROTEIN = (
    "pois chiche", "lentille", "feve", "haricot", "loubia", "fromage", "jben", "ricotta", "feta", "halloumi",
    "mozzarella", "gruyere", "emmental", "parmesan", "chevre", "labneh", "tofu",
)  # fmt: skip


def _words_re(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9œ])(?:" + "|".join(re.escape(w) for w in words) + ")")


_RED, _POULTRY, _FISH, _EGGS = _words_re(RED), _words_re(POULTRY), _words_re(FISH), _words_re(EGGS)
_PLANT, _NOT_DINNER_TITLE, _STAPLE = (
    _words_re(PLANT_PROTEIN),
    _words_re(NOT_DINNER_TITLES),
    _words_re(STAPLES),
)
PROTEINS = ("red", "poultry", "fish", "eggs", "veg")
ALL_SEASONS = frozenset({"hiver", "printemps", "ete", "automne"})

# Approximate lunar dates (± 1 day, to confirm every year), the same as the home row (lib/recipesHome.js).
LUNAR = (
    ("ramadan", date(2027, 2, 1), date(2027, 3, 9)),
    ("aid-fitr", date(2027, 3, 10), date(2027, 3, 14)),
    ("aid-kebir", date(2027, 5, 12), date(2027, 5, 23)),
)
SEASON_OF_MONTH = {12: "hiver", 1: "hiver", 2: "hiver", 3: "printemps", 4: "printemps", 5: "printemps",
                   6: "ete", 7: "ete", 8: "ete", 9: "automne", 10: "automne", 11: "automne"}  # fmt: skip


def moment_of(day: date) -> dict[str, str | None]:
    """{season, occasion}: « automne » / None; Ramadan, Aïd el-Fitr, Aïd el-Kébir on their dates."""
    occasion = next((key for key, start, end in LUNAR if start <= day <= end), None)
    return {"season": SEASON_OF_MONTH[day.month], "occasion": occasion}


# --- the candidates (pure) -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Cand:
    """A dish the week can use: its cost for N people, its words, what it buys."""

    id: str
    slug: str
    title: str
    cuisine: str
    category: str
    time_min: int
    protein: str
    high: float
    low: float
    tags: frozenset[str] = frozenset()
    seasons: frozenset[str] = frozenset()
    fresh: frozenset[Any] = frozenset()  # non-placard articles of the market (herbs, tomatoes, onions…)
    buys: frozenset[Any] = frozenset()  # every non-placard article it buys
    has_red: bool = False
    has_fish: bool = False
    have_hits: int = 0  # articles of « avec ce que j'ai » it uses
    # every meat / fish IN the dish, even hidden (minced meat in an « eggs » dish, chicken in an ojja)
    meats: frozenset[str] = frozenset()
    has_protein: bool = True  # meat, fish, eggs, legumes or cheese: an evening is a meal (owner 22/10)


@dataclass
class Options:
    nights: int
    budget: float | None = None
    quick: bool = False
    season: str | None = None
    occasion: str | None = None
    seed: int = 0
    seen: frozenset[str] = frozenset()  # dishes already shown this session: allowed, but last
    couscous_friday: bool = False  # a Tunisian family: the couscous on Friday (owner 22/10)
    home_cuisine: bool = False  # no cuisine wish: Tunisian / Maghrebi dishes the majority (QA 22/10 V1)
    recent: frozenset[str] = frozenset()  # last week's dishes of this phone: less likely (RECENT_MALUS)


@dataclass
class Plan:
    ok: bool
    days: list[Cand | None] = field(default_factory=list)
    reason: str | None = None
    loosened: list[str] = field(default_factory=list)


def proteins_in(lines: list[tuple[str, float]] | list[tuple[str, float, str]]) -> dict[str, Any]:
    """{main, meats, plant} from the lines' folded keys with their weight in kg (and their aisle: all of
    the « poissonnerie » is fish — daurade, merlan, loup…). `meats`: every red meat / poultry / fish present,
    whatever its weight; `plant`: another protein (legumes, cheese, a cold cut)."""
    weight = dict.fromkeys(PROTEINS, 0.0)
    meats: set[str] = set()
    plant = False
    for key, kg, *rest in lines:
        k = str(key)
        if "bouillon" in k:  # a stock cube is not the evening's meat
            continue
        if (
            "jambon" in k or "cachir" in k or "en tranche" in k
        ):  # a cold cut (sliced smoked turkey): a protein, not the evening's poultry
            plant = True
            continue
        if rest and rest[0] == "poissonnerie":
            kind = "fish"
        elif _RED.search(k) and not _POULTRY.search(k):
            kind = "red"
        elif _POULTRY.search(k):
            kind = "poultry"
        elif _FISH.search(k):
            kind = "fish"
        elif _EGGS.search(k):
            kind = "eggs"
        else:
            plant = plant or bool(_PLANT.search(k))
            continue
        weight[kind] += kg or (0.06 if kind == "eggs" else 0.05)
        if kind != "eggs":
            meats.add(kind)
    best = max(("red", "poultry", "fish", "eggs"), key=lambda p: weight[p])
    return {"main": best if weight[best] > 0 else "veg", "meats": frozenset(meats), "plant": plant}


def protein_of(lines: list[tuple[str, float]] | list[tuple[str, float, str]]) -> tuple[str, bool, bool]:
    """(main protein, has red meat, has fish) — see proteins_in."""
    p = proteins_in(lines)
    return p["main"], "red" in p["meats"], "fish" in p["meats"]


def evening_ok(c: Cand, opts: Options) -> bool:
    """Is the dish an evening of this week at all: its category, its tags and its name (no breakfast, side
    dish, drink, mezzé — owner 22/10), the moment."""
    if c.category not in DINNER or c.tags & NOT_DINNER_TAGS:
        return False
    if _NOT_DINNER_TITLE.search(recipes.fold(c.title)):
        return False
    if c.tags & STARTER_TAGS and not c.tags & MEAL_TAGS and opts.occasion != "ramadan":
        return False
    if c.category in RAMADAN_ONLY and opts.occasion != "ramadan":
        return False
    return not (c.category in SUMMER_ONLY and opts.season != "ete")


def _jitter(seed: int, cid: str) -> float:
    h = hashlib.blake2b(f"{seed}:{cid}".encode(), digest_size=4).digest()
    return int.from_bytes(h, "big") / 2**32 * JITTER


def week_seed(phone_seed: int, day: date, user: Any = None) -> int:
    """The draw's seed: the phone's own seed, the account and the ISO week (QA 22/10 V1): two families, or the
    same family two Mondays apart, get two different draws; the same phone the same week, the same week."""
    year, week, _ = day.isocalendar()
    h = hashlib.blake2b(f"{phone_seed}:{user or ''}:{year}-W{week}".encode(), digest_size=6).digest()
    return int.from_bytes(h, "big")


def _draw(scored: list[tuple[float, Cand]], seed: int, day: int) -> Cand:
    """One dish drawn among the near-best (DRAW_WINDOW), weighted by the score; same seed, same dish."""
    best = max(s for s, _ in scored)
    near = sorted(((s, c) for s, c in scored if s >= best - DRAW_WINDOW), key=lambda t: t[1].id)
    weights = [math.exp((s - best) / DRAW_TEMP) for s, _ in near]
    rng = random.Random(f"{seed}:{day}")  # a menu draw, not a secret
    return rng.choices([c for _, c in near], weights=weights, k=1)[0]


def _foreign_ok(c: Cand, chosen: list[Cand], opts: Options, level: int) -> bool:
    """A Tunisian family: another cuisine for at most nights // 3 evenings, until the protein rules are
    loosened (a quick foreign dish rather than a longer one on a weekday, QA 22/10 V3)."""
    if not opts.home_cuisine or level >= 2 or c.cuisine in MAGHREB:
        return True
    return sum(1 for x in chosen if x.cuisine not in MAGHREB) < max(1, opts.nights // 3)


def _max_red(nights: int, opts: Options) -> int:
    return (2 if nights >= 6 else 1) + (1 if opts.occasion == "aid-kebir" else 0)


def _max_poultry(nights: int) -> int:
    return 2 if nights <= 5 else 3


def _is_red(c: Cand) -> bool:
    return c.protein == "red" or "red" in c.meats or c.has_red


def _is_poultry(c: Cand) -> bool:
    return c.protein == "poultry" or "poultry" in c.meats


def _same_protein(a: Cand, b: Cand) -> bool:
    """The same main protein, or a meat / fish both contain (ojja au poulet then tajine au poulet)."""
    return a.protein == b.protein or bool(a.meats & b.meats)


def _time_ok(c: Cand, day: int, opts: Options, level: int) -> bool:
    if opts.quick:
        return c.time_min <= QUICK_MIN
    if day in LONG_DAYS:
        return True
    # Monday → Thursday: 45 min; 60 only as the last resort; never more (QA 22/10 V3: no 3 h on a Wednesday)
    return c.time_min <= (QUICK_MIN if level == 0 else 60)


def _variety_ok(
    c: Cand,
    neighbours: list[Cand],
    chosen: list[Cand],
    level: int,
    max_red: int,
    max_poultry: int = 99,
    home: bool = False,
) -> bool:
    """level 0: cuisine, category, protein all differ from the next evenings; 1: cuisine loosened; 2: protein
    too; 3: an evening without protein, the red-meat limit still kept; 4: only « never the same dish ». Red
    meat and poultry are counted from the INGREDIENTS (minced meat in an « eggs » dish is red meat); every
    evening has a protein (meat, fish, eggs, legumes, cheese)."""
    if any(c.id == x.id for x in chosen):
        return False
    if level >= 4:
        return True
    red_ok = not (_is_red(c) and sum(1 for x in chosen if _is_red(x)) >= max_red)
    if level >= 3:
        return red_ok  # the one red meat of the week is never broken before the very last resort
    if not c.has_protein:
        return False
    for n in neighbours:
        # a Tunisian family eats Tunisian two evenings in a row (QA 22/10 V1): only other cuisines alternate
        if level < 1 and c.cuisine and c.cuisine == n.cuisine and not (home and c.cuisine in MAGHREB):
            return False
        if level < 2 and _same_protein(c, n):
            return False
        if c.category == n.category:
            return False
    # the same kind of dish (two ojjas, two briks) once a week, twice when the rules are loosened
    if sum(1 for x in chosen if x.category == c.category) >= (1 if level < 2 else 2):
        return False
    if level < 2 and _is_poultry(c) and sum(1 for x in chosen if _is_poultry(x)) >= max_poultry:
        return False
    return red_ok


def score(c: Cand, day: int, chosen: list[Cand], opts: Options, share: float | None) -> float:
    """Higher is better. Shared articles, « avec ce que j'ai », the moment, the day, the cost."""
    s = 0.0
    bought = set().union(*(x.buys for x in chosen)) if chosen else set()
    fresh = set().union(*(x.fresh for x in chosen)) if chosen else set()
    # less waste, capped: the shared articles must not bring back the same few dishes every week (owner 22/10)
    s += min(6.0, 3.0 * len(c.fresh & fresh)) + min(2.0, 1.0 * len((c.buys - c.fresh) & bought))
    s += 4.0 * c.have_hits
    if opts.occasion == "ramadan":
        s += 5.0 if ("ramadan" in c.tags or "ramadan" in c.seasons) else 0.0
        s += 3.0 if c.category in ("soupe", "brik") else 0.0
    elif opts.occasion == "aid-kebir":
        s += 4.0 if (c.protein == "red" or "aïd" in c.tags) else 0.0
    if opts.season:
        if opts.season in c.seasons:
            s += 2.0
        elif c.seasons & ALL_SEASONS:
            s -= 2.0  # written for another season (a summer salad in winter)
        if c.category == "soupe":
            s += 2.0 if opts.season == "hiver" else (-3.0 if opts.season == "ete" else 0.0)
    festive = bool(c.tags & LONG_TAGS) or c.category in ("couscous", "plat de fête")
    if day in LONG_DAYS:
        # Friday and the weekend: the long, festive dishes (owner 22/10), never a 30-minute snack
        s += 4.0 if (c.time_min >= 50 or festive) else (-3.0 if c.time_min <= 30 else 0.0)
    else:
        s += 2.0 if (c.tags & QUICK_TAGS or c.time_min <= 30) else 0.0
        s -= 2.0 if (festive and not c.tags & QUICK_TAGS) else 0.0  # « Week-end, Invités » on a Thursday
    if opts.couscous_friday and c.category == "couscous":
        s += 10.0 if day == 4 else -6.0  # the couscous waits for Friday
    if c.cuisine == "tunisienne":
        s += 2.0
        s += 2.5 if _STAPLE.search(recipes.fold(c.slug.replace("-", " "))) else 0.0
    elif opts.home_cuisine and c.cuisine in MAGHREB:
        s += 1.0
    avg = opts.budget / opts.nights if opts.budget else None  # the evening's part of the whole week
    dear = (share is not None and share > 0 and c.high > 1.25 * share) or (
        avg is not None and c.high > 1.5 * avg
    )
    if c.protein == "fish" and not dear and not any(x.protein == "fish" for x in chosen):
        s += 3.0
    if c.protein in ("veg", "eggs") and not c.meats and not any(x.protein in ("veg", "eggs") for x in chosen):
        s += 2.0  # one meatless evening (legumes, eggs) in the week
    if share is not None and share > 0:
        if c.high > share:
            s -= 6.0 * (c.high - share) / share
        if avg is not None and c.high > 1.5 * avg:  # a 34 DT merlan in an 80 DT week: no (owner 22/10)
            s -= 4.0 + 6.0 * (c.high - 1.5 * avg) / avg
    else:
        s -= 0.08 * c.high
    if c.id in opts.seen:
        s -= SEEN_MALUS
    if c.id in opts.recent:
        s -= RECENT_MALUS
    return s + _jitter(opts.seed, c.id)


def _reserve(pool_by_cost: list[Cand], skip: set[str], days_left: list[int], opts: Options) -> float:
    """The money the evenings left need at least: a weekday its cheapest QUICK dish (≤ 45 min), so that the
    budget never leaves Thursday only a long stew (QA 22/10 V3); the other days any. Too few quick dishes →
    the plain cheapest ones."""
    used = set(skip)
    total = 0.0
    for d in sorted(days_left, key=lambda x: x in LONG_DAYS and not opts.quick):
        quick = opts.quick or d not in LONG_DAYS
        pick = next(
            (c for c in pool_by_cost if c.id not in used and (not quick or c.time_min <= QUICK_MIN)), None
        )
        if pick is None:
            return _cheapest(pool_by_cost, skip, len(days_left))
        used.add(pick.id)
        total += pick.high
    return total


def _cheapest(pool_by_cost: list[Cand], skip: set[str], k: int) -> float:
    total, n = 0.0, 0
    for c in pool_by_cost:
        if n >= k:
            break
        if c.id in skip:
            continue
        total += c.high
        n += 1
    return total if n >= k else math.inf


LOOSEN = ("cuisine", "protein", "category")
# The order the rules are loosened in, (time level, variety level). Monday → Thursday: ≤ 45 min is kept while
# the cuisine wish AND the protein rules are loosened (the same protein as a neighbour before a longer dish,
# QA 22/10 V3, « Changer » too); up to 60 min only as the last resort, then an evening without protein; never
# more than 60 min on a weekday (nothing fits → « few », or « no_other » for « Changer »).
STEPS = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3), (0, 4), (1, 4))


def plan_week(
    pool: list[Cand],
    opts: Options,
    fixed: dict[int, Cand] | None = None,
    avoid: set[str] | None = None,
    cheap: bool = False,
) -> Plan:
    """The greedy choice, evening by evening (Monday first). `fixed`: the kept evenings (padlock, or every
    other evening when one is changed); `avoid`: dishes not to propose again (« Changer »); `cheap`: the
    cheapest week that keeps the rules (to say the smallest budget). The budget first keeps a quick dish for
    each weekday left; when no week fits that way, the plain cheapest dishes (a 60-min stew on Thursday)."""
    first = _plan_week(pool, opts, fixed, avoid, cheap, quick_reserve=True)
    if first.ok or cheap or opts.budget is None:
        return first
    return _plan_week(pool, opts, fixed, avoid, cheap, quick_reserve=False)


def _plan_week(
    pool: list[Cand],
    opts: Options,
    fixed: dict[int, Cand] | None,
    avoid: set[str] | None,
    cheap: bool,
    quick_reserve: bool,
) -> Plan:
    fixed = dict(fixed or {})
    avoid = set(avoid or ())
    days: list[Cand | None] = [fixed.get(d) for d in range(opts.nights)]
    # never a dish that cannot be ordered alone: one order is at most BASKET_CAP DT (QA 22/10 V4, couscous au
    # poisson 86.5 DT with « Libre »)
    pool = [c for c in pool if evening_ok(c, opts) and c.id not in avoid and c.high <= BASKET_CAP]
    # the money kept for the evenings left: the cheapest dishes WITH a protein (a 3 DT potato stew is not a
    # dinner the budget can count on — QA 22/10 V2), all of them only when there are too few
    meals = [c for c in pool if c.has_protein]
    by_cost = sorted(meals if len(meals) >= opts.nights else pool, key=lambda c: c.high)
    max_red = _max_red(opts.nights, opts)
    max_poultry = _max_poultry(opts.nights)
    loosened: set[str] = set()
    # Friday first when it is the couscous evening (so that the budget keeps room for it), then Monday → …
    order = list(range(opts.nights))
    if opts.couscous_friday and opts.nights > 4:
        order = [4, *[d for d in order if d != 4]]
    for day in order:
        if days[day] is not None:
            continue
        chosen = [c for c in days if c is not None]
        neighbours = [
            x for x in (days[day - 1] if day else None, days[day + 1] if day + 1 < opts.nights else None) if x
        ]
        days_left = [d for d in range(opts.nights) if days[d] is None and d != day]
        left = len(days_left)
        spent = sum(c.high for c in chosen)
        best: Cand | None = None
        for t_level, level in STEPS:
            scored: list[tuple[float, Cand]] = []
            for c in pool:
                if not _time_ok(c, day, opts, t_level) or not _variety_ok(
                    c, neighbours, chosen, level, max_red, max_poultry, opts.home_cuisine
                ):
                    continue
                if not _foreign_ok(c, chosen, opts, level):
                    continue
                if opts.budget is not None and not cheap:
                    if level < 4 and c.high > DEAR * opts.budget / opts.nights:
                        continue  # one evening eating twice its part of a tight week (merlan 34 DT of 80)
                    skip = {x.id for x in chosen} | {c.id}
                    reserve = (
                        _reserve(by_cost, skip, days_left, opts)
                        if quick_reserve
                        else _cheapest(by_cost, skip, left)
                    )
                    if spent + c.high + reserve > opts.budget + 1e-9:
                        continue
                share = (opts.budget - spent) / (left + 1) if opts.budget is not None else None
                sc = -c.high * 10 + _jitter(opts.seed, c.id) if cheap else score(c, day, chosen, opts, share)
                scored.append((sc, c))
            if scored:
                if cheap:
                    # the cheapest week the real draw can reach: the couscous on Friday, the long dishes
                    # (which never fit a weekday) on Friday and the weekend
                    pick = scored
                    if opts.couscous_friday and day == 4:
                        pick = [t for t in pick if t[1].category == "couscous"] or pick
                    if day in LONG_DAYS and not opts.quick:
                        pick = [t for t in pick if t[1].time_min > 60] or pick
                    best = max(pick, key=lambda t: t[0])[1]
                else:
                    # « Une autre semaine » never goes back: the dishes already shown only if nothing else
                    fresh = [t for t in scored if t[1].id not in opts.seen] or scored
                    if opts.couscous_friday and day == 4:  # the couscous on Friday whenever one fits
                        fresh = [t for t in fresh if t[1].category == "couscous"] or fresh
                    best = _draw(fresh, opts.seed, day)
                loosened.update(LOOSEN[:level])
                break
        if best is None:
            return Plan(
                ok=False, days=days, reason="budget" if opts.budget is not None and not cheap else "few"
            )
        days[day] = best
    if opts.budget is not None and not cheap and sum(c.high for c in days if c) > opts.budget + 1e-9:
        return Plan(ok=False, days=days, reason="budget")  # the kept evenings alone are above it
    return Plan(ok=True, days=days, loosened=[w for w in LOOSEN if w in loosened])


RECENT_BLOCK = 13  # weeks (a quarter): the chain of proposals is recomputed from the start of its block


def _week_opts(opts: Options, phone_seed: int, day: date, user: Any, recent: frozenset[str]) -> Options:
    moment = moment_of(day)
    return replace(
        opts,
        seed=week_seed(phone_seed, day, user),
        seen=frozenset(),
        recent=recent,
        season=moment["season"],
        occasion=moment["occasion"],
        couscous_friday=opts.couscous_friday and moment["occasion"] != "ramadan",
    )


def _draw_week(pool: list[Cand], opts: Options, recent_weeks: list[frozenset[str]], **kw: Any) -> Plan:
    """plan_week, drawn again (up to 3 times, another seed) when it gives one of the recent weeks."""
    p = plan_week(pool, opts, **kw)
    for k in range(3):
        if not p.ok or frozenset(c.id for c in p.days if c) not in recent_weeks:
            break
        p = plan_week(pool, replace(opts, seed=opts.seed + k + 1), **kw)
    return p


def recent_weeks(
    pool: list[Cand], opts: Options, phone_seed: int, today: date, user: Any = None
) -> list[frozenset[str]]:
    """The proposals this phone got the previous weeks of its block of RECENT_BLOCK weeks, oldest first (QA
    22/10 V1: the same family never gets the same week again). Nothing is stored: the chain is recomputed
    exactly from the start of the block (≤ 12 small draws of ~10 ms); each week: the dishes of the two weeks
    before weigh less (`recent`), and it is never one of the weeks before (drawn again)."""
    ordinal = today.toordinal() // 7
    first = ordinal - ordinal % RECENT_BLOCK
    weeks: list[frozenset[str]] = []
    for w in range(first, ordinal):
        day = today - timedelta(days=7 * (ordinal - w))
        o = _week_opts(opts, phone_seed, day, user, _recent_dishes(weeks))
        p = _draw_week(pool, o, weeks)
        weeks.append(frozenset(c.id for c in p.days if c) if p.ok else frozenset())
    return weeks


def _recent_dishes(weeks: list[frozenset[str]]) -> frozenset[str]:
    """The dishes of the last two weeks: they weigh less (RECENT_MALUS)."""
    return frozenset().union(*weeks[-2:]) if weeks else frozenset()


def min_budget(pool: list[Cand], opts: Options, fixed: dict[int, Cand] | None = None) -> float | None:
    """The smallest budget that works (the cheapest week under the same rules), rounded up to 5 DT."""
    cheap = plan_week(pool, Options(**{**opts.__dict__, "budget": None}), fixed, cheap=True)
    if not cheap.ok:
        return None
    total = sum(c.high for c in cheap.days if c)
    return float(math.ceil(total / MIN_BUDGET_STEP) * MIN_BUDGET_STEP)


# --- the server side --------------------------------------------------------------------------------------


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _budget(value: Any) -> float | None:
    if value in (None, "", "libre", 0, "0"):
        return None
    try:
        b = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_budget", "budget", min=BUDGET_MIN, max=BUDGET_MAX) from None
    if not math.isfinite(b) or b < BUDGET_MIN or b > BUDGET_MAX:
        raise ApiError(400, "invalid_budget", "budget", min=BUDGET_MIN, max=BUDGET_MAX)
    return b


def _have(value: Any) -> set[int]:
    out: set[int] = set()
    if isinstance(value, list):
        for v in value[:120]:
            if isinstance(v, dict):
                v = v.get("id")
            if isinstance(v, bool):
                continue
            if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
                out.add(int(v))
    return out


def _words(values: Any) -> frozenset[str]:
    return frozenset(recipes.fold(v) for v in (values or []) if str(v).strip())


async def _pool(
    session: AsyncSession, people: int, have: set[int], cuisines: list[str], no_red: bool, no_fish: bool
) -> tuple[list[Cand], recipes.Loaded, Any]:
    """Every dish with a known cost for `people` (+ the « avec ce que j'ai » articles at home), the wishes
    applied (cuisines, no red meat, no fish)."""
    where = [
        Recipe.status.in_(recipes.BROWSABLE),
        Recipe.servings_unit == "people",
        Recipe.category.in_(DINNER),
        Recipe.time_min.is_not(None),
    ]
    if cuisines:
        where.append(Recipe.cuisine.in_(cuisines))
    ids = list((await session.execute(select(Recipe.id).where(*where).limit(POOL_MAX))).scalars())
    data = await recipes.load(session, ids=ids, published_only=False)
    prices = await recipe_prices.prices(session) if ids else None
    out: list[Cand] = []
    for r in data.recipes:
        at_home = recipes._pantry_defaults(data, [r.id]) | have
        lst = recipes.shopping(data, [(r.id, people)], at_home)
        c = recipe_prices.cost(lst["buy"], prices)
        if not c.get("show_total") or c.get("high") is None:
            continue
        keys_kg: list[tuple[str, float, str]] = []
        used_have = 0
        for line in data.lines.get(r.id, []):
            if line.optional:
                # not bought nor counted (QA 22/10 V2): the optional chicken of a marqa batata is not the
                # evening's protein, nor a red meat / poultry for the limits and « Sans viande rouge »
                continue
            ing, q = recipes.line_need(data, line, ru.scale(line.qty, people, r.base_servings))
            w = ru.weight_kg(q, ing.unit, ing)
            keys_kg.append(
                (recipes.fold(ing.key, line.name_fr), float(w) if w is not None else 0.0, ing.aisle)
            )
            if ing.id in have and not ing.pantry:
                used_have += 1
        info = proteins_in(keys_kg)
        protein, has_red, has_fish = info["main"], "red" in info["meats"], "fish" in info["meats"]
        if (no_red and has_red) or (no_fish and has_fish):
            continue
        category = (r.category or "").strip().lower()
        buys = frozenset(ing.id for ing, _ in lst["buy"] if not ing.pantry)
        fresh = frozenset(ing.id for ing, _ in lst["buy"] if not ing.pantry and ing.aisle == "marche")
        out.append(
            Cand(
                id=str(r.id),
                slug=r.slug,
                title=r.title_fr,
                cuisine=r.cuisine or "",
                category=category,
                time_min=int(r.time_min or 0),
                protein=protein,
                high=float(c["high"]),
                low=float(c["low"]),
                tags=_words(r.tags),
                seasons=frozenset(r.seasons or []),
                fresh=fresh,
                buys=buys,
                has_red=has_red,
                has_fish=has_fish,
                have_hits=used_have,
                meats=info["meats"],
                has_protein=protein != "veg" or info["plant"] or category == "légumineuses",
            )
        )
    return out, data, prices


def _fixed(raw: Any, nights: int) -> dict[int, str]:
    out: dict[int, str] = {}
    if isinstance(raw, list):
        for item in raw[:NIGHTS_MAX]:
            if not isinstance(item, dict):
                continue
            day = _int(item.get("day"), -1, -1, NIGHTS_MAX)
            rid = recipes._uuid(item.get("recipe_id"))
            if 0 <= day < nights and rid is not None:
                out[day] = str(rid)
    return out


def _merged(
    data: recipes.Loaded, prices: Any, ids: list[str], people: int, at_home: set[Any]
) -> dict[str, Any]:
    lst = recipes.shopping(data, [(uuid.UUID(i), people) for i in ids], at_home)
    return recipe_prices.cost(lst["buy"], prices)


def _baskets(
    data: recipes.Loaded, prices: Any, days: list[Cand], people: int, at_home: set[Any]
) -> list[dict]:
    """The evenings cut, in order, into orders of at most BASKET_CAP DT (a dish above it alone: its own)."""
    out: list[dict[str, Any]] = []
    group: list[int] = []
    cost: dict[str, Any] | None = None
    for i in range(len(days)):
        trial = [*group, i]
        c = _merged(data, prices, [days[d].id for d in trial], people, at_home)
        if group and (c.get("high") is None or c["high"] > BASKET_CAP):
            out.append({"days": group, "low": cost["low"], "high": cost["high"]})
            group, cost = [i], _merged(data, prices, [days[i].id], people, at_home)
        else:
            group, cost = trial, c
    if group:
        out.append({"days": group, "low": cost["low"], "high": cost["high"]})
    return out


async def plan(session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]) -> dict[str, Any]:
    """{people, nights, budget? (null = libre), cuisines? [..], no_red_meat?, no_fish?, quick?, have? [ids],
    fixed? [{day, recipe_id}], avoid? [recipe ids], seen? [recipe ids shown this session: proposed last],
    seed?, today? (YYYY-MM-DD)} →
    ok: {days: [{day, card, shared[]}], total {low, high}, separate_high, savings, shared [{id, name_fr,
    name_ar, nights}], baskets [{days, low, high}], at_home, loosened, moment}
    not ok: {reason: 'budget' (+ min_budget, or null when even without a budget nothing fits) | 'few'
    (too few dishes for these wishes) | 'no_other' (« Changer »: nothing else fits that evening; `quick_day`:
    a weekday, where only dishes of 45 min — 60 at most — are proposed)}."""
    await recipes._require_see(session, viewer)
    people = _int(payload.get("people"), 4, PEOPLE_MIN, PEOPLE_MAX)
    nights = _int(payload.get("nights"), 5, NIGHTS_MIN, NIGHTS_MAX)
    budget = _budget(payload.get("budget"))
    raw_cuisines = payload.get("cuisines") if isinstance(payload.get("cuisines"), list) else []
    cuisines = [str(c).strip()[:60] for c in raw_cuisines[:30] if str(c).strip()]
    have = _have(payload.get("have"))
    try:
        today = date.fromisoformat(str(payload.get("today"))[:10]) if payload.get("today") else date.today()
    except ValueError:
        today = date.today()
    moment = moment_of(today)
    raw_seen = payload.get("seen") if isinstance(payload.get("seen"), list) else []
    home = not cuisines
    phone_seed, user = _int(payload.get("seed"), 0, 0, 10**9), (viewer.id if viewer else None)
    opts = Options(
        nights=nights,
        budget=budget,
        quick=payload.get("quick") is True,
        season=moment["season"],
        occasion=moment["occasion"],
        # the phone's seed, the account AND the week of the year: another family, or next week, another week
        seed=week_seed(phone_seed, today, user),
        seen=frozenset(str(x) for x in raw_seen[:SEEN_MAX]),
        couscous_friday=nights > 4
        and moment["occasion"] != "ramadan"
        and (not cuisines or "tunisienne" in [c.lower() for c in cuisines]),
        home_cuisine=home,
    )
    pool, data, prices = await _pool(
        session, people, have, cuisines, payload.get("no_red_meat") is True, payload.get("no_fish") is True
    )
    recent = recent_weeks(pool, opts, phone_seed, today, user)
    opts.recent = _recent_dishes(recent)
    by_id = {c.id: c for c in pool}
    fixed = {d: by_id[i] for d, i in _fixed(payload.get("fixed"), nights).items() if i in by_id}
    raw_avoid = payload.get("avoid") if isinstance(payload.get("avoid"), list) else []
    avoid = {str(x) for x in raw_avoid[:60]}
    changing = len(fixed) == nights - 1 and bool(avoid)
    base = {
        "success": True,
        "people": people,
        "nights": nights,
        "budget": budget,
        "moment": moment,
        "pool": len(pool),
    }
    result = _draw_week(pool, opts, [] if fixed or avoid or opts.seen else recent, fixed=fixed, avoid=avoid)
    if not result.ok and changing:
        day = next(d for d in range(nights) if d not in fixed)
        # a weekday: no other QUICK dish fits (never a 3 h dish on a Wednesday, QA 22/10 V3) — said as such
        return {**base, "ok": False, "reason": "no_other", "quick_day": day not in LONG_DAYS}
    if not result.ok and result.reason == "budget":
        least = min_budget(pool, opts, fixed)
        if least is not None:
            return {**base, "ok": False, "reason": "budget", "min_budget": least}
    if not result.ok:
        return {**base, "ok": False, "reason": "few"}
    days: list[Cand] = [c for c in result.days if c is not None]
    ids = [c.id for c in days]
    at_home = recipes._pantry_defaults(data, [uuid.UUID(i) for i in ids]) | have
    total = _merged(data, prices, ids, people, at_home)
    separate = sum(c.high for c in days)
    counts: dict[Any, int] = {}
    for c in days:
        for i in c.buys:
            counts[i] = counts.get(i, 0) + 1
    names = {ing.id: ing for ing in data.ings.values()}
    shared = [
        {"id": i, "name_fr": names[i].name_fr, "name_ar": names[i].name_ar, "nights": n}
        for i, n in sorted(counts.items(), key=lambda t: (-t[1], str(t[0])))
        if n >= 2 and i in names and i not in have
    ]
    recipe_rows = data.by_id
    out_days = []
    for d, c in enumerate(days):
        r = recipe_rows[uuid.UUID(c.id)]
        card_cost = _merged(data, prices, [c.id], people, recipes._pantry_defaults(data, [r.id]) | have)
        others = set().union(*(x.buys for j, x in enumerate(days) if j != d)) if len(days) > 1 else set()
        out_days.append(
            {
                "day": d,
                "card": recipes._card(r, card_cost, len(data.lines.get(r.id, [])), people),
                "protein": c.protein,
                "shared": [
                    {"id": s["id"], "name_fr": s["name_fr"], "name_ar": s["name_ar"]}
                    for s in shared
                    if s["id"] in c.buys and s["id"] in others
                ][:4],
            }
        )
    high = total.get("high")
    return {
        **base,
        "ok": True,
        "days": out_days,
        "total": {"low": total.get("low"), "high": high, "show_total": bool(total.get("show_total"))},
        "cost": recipes._cost_doc(total),
        "separate_high": round(separate, 3),
        "savings": round(max(0.0, separate - float(high)), 1) if high is not None else 0.0,
        "shared": shared[:12],
        "baskets": _baskets(data, prices, days, people, at_home),
        "basket_cap": BASKET_CAP,
        "at_home": sorted(at_home, key=str),
        "loosened": result.loosened,
    }
