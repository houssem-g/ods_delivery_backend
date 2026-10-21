"""« Ma semaine » (Recettes, owner 21/10: « j'accepte tout, exécute » — in the FIRST version): the customer
says how many people, how many evenings (3 to 7), a budget for the week (or none) and a few wishes; the
server proposes one dinner per evening by RULES (a greedy choice, no AI), each evening replaceable
(« Changer ») or kept (the padlock), then « Tout mettre dans Ma liste ».

Rules (one place, tested in tests/test_recipes_week.py):
- Only recipes whose cost is KNOWN for N people (a total shown: ≥ 70 % of the articles and of the weight
  priced, recipe_prices.cost), dishes counted in people, and an evening category (a stew, a couscous, fish,
  pasta, eggs, a soup — soups and briks first in Ramadan, salads only in summer; never a pastry, a drink,
  a sauce).
- Variety: never the same dish twice; two evenings side by side never share the cuisine, the category nor the
  main protein (red meat / poultry / fish / eggs / vegetarian); at most one red meat (two from 6 evenings).
  When the wishes leave too few dishes, the rules are loosened in this order: cuisine, protein, category.
- Monday to Thursday: ready in 45 min or less (loosened to 60, then any, only if nothing fits); Friday and the
  weekend: the longer dishes first (couscous, a dish « Week-end » / « Fête » / « Repas du vendredi »).
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
import uuid
from dataclasses import dataclass, field
from datetime import date
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
LONG_TAGS = frozenset({"week-end", "fête", "invités", "repas du vendredi", "vendredi", "repas de famille"})
QUICK_TAGS = frozenset({"rapide", "quotidien"})

# The main protein, from the dictionary keys of the lines (folded: no accent, no case).
RED = ("agneau", "boeuf", "veau", "mouton", "viande hachee", "merguez", "kaddid", "tripes", "kefta", "osbane")
POULTRY = ("poulet", "dinde", "caille", "volaille", "pigeon")
FISH = ("thon", "anchois", "sardine", "poisson", "crevette", "calamar", "seiche", "poulpe", "moule", "morue")
EGGS = ("oeuf",)
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


@dataclass
class Options:
    nights: int
    budget: float | None = None
    quick: bool = False
    season: str | None = None
    occasion: str | None = None
    seed: int = 0


@dataclass
class Plan:
    ok: bool
    days: list[Cand | None] = field(default_factory=list)
    reason: str | None = None
    loosened: list[str] = field(default_factory=list)


def protein_of(lines: list[tuple[str, float]] | list[tuple[str, float, str]]) -> tuple[str, bool, bool]:
    """(main protein, has red meat, has fish) from the lines' folded keys with their weight in kg (and their
    aisle: everything of the « poissonnerie » is fish — daurade, merlan, loup…)."""
    weight = dict.fromkeys(PROTEINS, 0.0)
    has_red = has_fish = False
    for key, kg, *rest in lines:
        k = f" {key} "
        if rest and rest[0] == "poissonnerie":
            weight["fish"] += kg or 0.05
            has_fish = True
        elif any(w in k for w in RED):
            weight["red"] += kg or 0.05
            has_red = True
        elif any(w in k for w in POULTRY) and "bouillon" not in k:
            weight["poultry"] += kg or 0.05
        elif any(w in k for w in FISH):
            weight["fish"] += kg or 0.05
            has_fish = True
        elif any(w in k for w in EGGS):
            weight["eggs"] += kg or 0.06
    best = max(("red", "poultry", "fish", "eggs"), key=lambda p: weight[p])
    return (best if weight[best] > 0 else "veg"), has_red, has_fish


def evening_ok(c: Cand, opts: Options) -> bool:
    """Is the dish an evening of this week at all (category and moment)."""
    if c.category not in DINNER:
        return False
    if c.category in RAMADAN_ONLY and opts.occasion != "ramadan":
        return False
    return not (c.category in SUMMER_ONLY and opts.season != "ete")


def _jitter(seed: int, cid: str) -> float:
    h = hashlib.blake2b(f"{seed}:{cid}".encode(), digest_size=4).digest()
    return int.from_bytes(h, "big") / 2**32 * 1.5


def _max_red(nights: int, opts: Options) -> int:
    return (2 if nights >= 6 else 1) + (1 if opts.occasion == "aid-kebir" else 0)


def _time_ok(c: Cand, day: int, opts: Options, level: int) -> bool:
    if opts.quick:
        return c.time_min <= QUICK_MIN
    if day in LONG_DAYS:
        return True
    return level >= 2 or c.time_min <= (QUICK_MIN if level == 0 else 60)


def _variety_ok(c: Cand, neighbours: list[Cand], chosen: list[Cand], level: int, max_red: int) -> bool:
    """level 0: cuisine, category, protein all differ from the next evenings; 1: cuisine loosened; 2: protein
    too; 3: only « never the same dish »."""
    if any(c.id == x.id for x in chosen):
        return False
    if level >= 3:
        return True
    for n in neighbours:
        if level < 1 and c.cuisine and c.cuisine == n.cuisine:
            return False
        if level < 2 and c.protein == n.protein:
            return False
        if c.category == n.category:
            return False
    # the same kind of dish (two ojjas, two briks) once a week, twice when the rules are loosened
    if sum(1 for x in chosen if x.category == c.category) >= (1 if level < 2 else 2):
        return False
    return not (c.protein == "red" and sum(1 for x in chosen if x.protein == "red") >= max_red)


def score(c: Cand, day: int, chosen: list[Cand], opts: Options, share: float | None) -> float:
    """Higher is better. Shared articles, « avec ce que j'ai », the moment, the day, the cost."""
    s = 0.0
    bought = set().union(*(x.buys for x in chosen)) if chosen else set()
    fresh = set().union(*(x.fresh for x in chosen)) if chosen else set()
    s += 3.0 * len(c.fresh & fresh) + 1.0 * len((c.buys - c.fresh) & bought)
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
    if day in LONG_DAYS:
        s += (
            3.0
            if (c.time_min >= 60 or c.tags & LONG_TAGS or c.category in ("couscous", "plat de fête"))
            else 0.0
        )
    else:
        s += 2.0 if (c.tags & QUICK_TAGS or c.time_min <= 30) else 0.0
    if c.cuisine == "tunisienne":
        s += 2.0
    if c.protein == "fish" and not any(x.protein == "fish" for x in chosen):
        s += 3.0
    if c.protein in ("veg", "eggs") and not any(x.protein in ("veg", "eggs") for x in chosen):
        s += 2.0
    if share is not None and share > 0:
        if c.high > share:
            s -= 4.0 * (c.high - share) / share
    else:
        s -= 0.08 * c.high
    return s + _jitter(opts.seed, c.id)


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


def plan_week(
    pool: list[Cand],
    opts: Options,
    fixed: dict[int, Cand] | None = None,
    avoid: set[str] | None = None,
    cheap: bool = False,
) -> Plan:
    """The greedy choice, evening by evening (Monday first). `fixed`: the kept evenings (padlock, or every
    other evening when one is changed); `avoid`: dishes not to propose again (« Changer »); `cheap`: the
    cheapest week that keeps the rules (to say the smallest budget)."""
    fixed = dict(fixed or {})
    avoid = set(avoid or ())
    days: list[Cand | None] = [fixed.get(d) for d in range(opts.nights)]
    pool = [c for c in pool if evening_ok(c, opts) and c.id not in avoid]
    by_cost = sorted(pool, key=lambda c: c.high)
    max_red = _max_red(opts.nights, opts)
    loosened: set[str] = set()
    for day in range(opts.nights):
        if days[day] is not None:
            continue
        chosen = [c for c in days if c is not None]
        neighbours = [
            x for x in (days[day - 1] if day else None, days[day + 1] if day + 1 < opts.nights else None) if x
        ]
        left = sum(1 for d in range(day + 1, opts.nights) if days[d] is None)
        spent = sum(c.high for c in chosen)
        best: tuple[float, Cand] | None = None
        for level in range(4):
            for c in pool:
                if not _time_ok(c, day, opts, min(level, 2)) or not _variety_ok(
                    c, neighbours, chosen, level, max_red
                ):
                    continue
                if opts.budget is not None and not cheap:
                    reserve = _cheapest(by_cost, {x.id for x in chosen} | {c.id}, left)
                    if spent + c.high + reserve > opts.budget + 1e-9:
                        continue
                share = (opts.budget - spent) / (left + 1) if opts.budget is not None else None
                sc = -c.high * 10 + _jitter(opts.seed, c.id) if cheap else score(c, day, chosen, opts, share)
                if best is None or sc > best[0]:
                    best = (sc, c)
            if best is not None:
                loosened.update(LOOSEN[:level])
                break
        if best is None:
            return Plan(
                ok=False, days=days, reason="budget" if opts.budget is not None and not cheap else "few"
            )
        days[day] = best[1]
    if opts.budget is not None and not cheap and sum(c.high for c in days if c) > opts.budget + 1e-9:
        return Plan(ok=False, days=days, reason="budget")  # the kept evenings alone are above it
    return Plan(ok=True, days=days, loosened=[w for w in LOOSEN if w in loosened])


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
            ing, q = recipes.line_need(data, line, ru.scale(line.qty, people, r.base_servings))
            w = ru.weight_kg(q, ing.unit, ing)
            keys_kg.append(
                (recipes.fold(ing.key, line.name_fr), float(w) if w is not None else 0.0, ing.aisle)
            )
            if ing.id in have and not ing.pantry:
                used_have += 1
        protein, has_red, has_fish = protein_of(keys_kg)
        if (no_red and has_red) or (no_fish and has_fish):
            continue
        buys = frozenset(ing.id for ing, _ in lst["buy"] if not ing.pantry)
        fresh = frozenset(ing.id for ing, _ in lst["buy"] if not ing.pantry and ing.aisle == "marche")
        out.append(
            Cand(
                id=str(r.id),
                slug=r.slug,
                title=r.title_fr,
                cuisine=r.cuisine or "",
                category=(r.category or "").strip().lower(),
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
    fixed? [{day, recipe_id}], avoid? [recipe ids], seed?, today? (YYYY-MM-DD)} →
    ok: {days: [{day, card, shared[]}], total {low, high}, separate_high, savings, shared [{id, name_fr,
    name_ar, nights}], baskets [{days, low, high}], at_home, loosened, moment}
    not ok: {reason: 'budget' (+ min_budget, or null when even without a budget nothing fits) | 'few'
    (too few dishes for these wishes) | 'no_other' (« Changer »: nothing else fits that evening)}."""
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
    opts = Options(
        nights=nights,
        budget=budget,
        quick=payload.get("quick") is True,
        season=moment["season"],
        occasion=moment["occasion"],
        seed=_int(payload.get("seed"), 0, 0, 10**9),
    )
    pool, data, prices = await _pool(
        session, people, have, cuisines, payload.get("no_red_meat") is True, payload.get("no_fish") is True
    )
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
    result = plan_week(pool, opts, fixed, avoid)
    if not result.ok and changing:
        return {**base, "ok": False, "reason": "no_other"}
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
