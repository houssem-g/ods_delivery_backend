"""« Avec ce que j'ai » (Recettes, owner 21/10): the customer lists what he has, the app tells which recipes
he can make. EXACT rules, no AI, computed here on the server over every browsable recipe (547 on 21/10).

Rules (one place, tested in tests/test_recipes_pantry.py):
- An ingredient is a dictionary id (`ingredients`, 256 on 21/10). The customer's « j'ai » list is ids, or
  words resolved here through the dictionary: French / Arabic names, the darija words and the synonyms of
  the referential file (`recipes_pantry_words.json`, generated from data/recipes/ingredients_tn.json —
  « tmatem », « bsal », « felfel… »), folded (no case, no accent, Arabic letters unified).
- A few families count as the same thing in a kitchen (FAMILIES: a chicken, chickpeas dry or canned, rice,
  milk, short or long pasta, grated cheese): having one of them is having the others.
- The « placard » (dictionary `pantry`: oil, salt, spices, harissa, flour…, or a line the recipe marks
  « placard ») is present by default; the customer can take some out (`pantry_off`) or all (`use_pantry`
  false).
- An optional line never counts as missing.
- A recipe is shown only if the customer has at least ONE of its non-placard ingredients himself (oil and
  salt alone are not « avec ce que j'ai »; `have_n` counts these only), and only with 0, 1, 2 or 3 missing
  ingredients. Groups « Vous avez tout », « Il manque 1 », « 2 », « 3 »; inside a group: the most of HIS
  ingredients used first, then the quickest, then the title.
- The listRecipes filters apply (cuisine — one or a list —, country, category, tag, max_time, q).

Each card is the listRecipes card (cost for the recipe's own people, photo…) plus `have_n`, `missing`
[{id, name_fr, name_ar}], `missing_cost` (the honest cost of the missing articles only, same rules: no total
under 70 % priced) and `at_home` (the ids to send as « j'ai déjà » to « Ma liste », so that « Me faire
livrer ce qui manque » buys the missing articles only — optional and placard lines included in it).

Visibility: like every recipe route, ADMINS ONLY for now (recipes._require_see).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ingredient, Recipe, RecipeLine
from app.security.deps import CurrentUser
from app.services import recipe_prices, recipes
from app.services import recipe_units as ru

MAX_MISSING = 3
PER_GROUP = 30
PER_GROUP_MAX = 60
MAX_HAVE = 120
WORDS_FILE = Path(__file__).with_name("recipes_pantry_words.json")

# Same thing in a kitchen (dictionary keys). Having one = having the others.
FAMILIES: tuple[tuple[str, ...], ...] = (
    ("poulet entier", "cuisses de poulet", "escalope de poulet"),
    ("pois chiches", "pois chiches en conserve"),
    ("riz", "riz basmati"),
    ("lait demi ecreme", "lait entier"),
    ("spaghetti", "macaroni", "coquillettes", "langues d oiseau"),
    ("fromage rape", "gruyere"),
    # two products with their own price (bell pepper 5, long « felfel » 153), the same use in a Tunisian pan
    ("poivron vert", "piment doux long"),
)
# « Ajout rapide » of the screen, in this order (dictionary keys; missing keys are skipped).
QUICK = (
    "oeufs",
    "tomate",
    "oignon",
    "pomme de terre",
    "pois chiches",
    "macaroni",
    "riz",
    "poulet entier",
    "thon a l huile",
    "viande hachee",
    "merguez",
    "courgette",
    "carotte",
    "poivron vert",
    "citron",
    "persil",
    "lentilles",
    "fromage rape",
)


def _key(value: Any) -> str:
    """A dictionary key folded: « huile d'olive » == « huile d olive »."""
    return " ".join(recipes.fold(value).split())


def singular(key: str) -> str:
    """Each word without its plural « s » / « x » (QA 22/10: « pommes de terre », « haricot vert »,
    « cuisse de poulet » → the dictionary's names). Same rule as the phone (recipesPantry.js `singular`)."""
    return " ".join(
        w[:-1] if len(w) > 3 and w[-1] in "sx" and w[-2].isascii() and w[-2].isalpha() else w
        for w in key.split()
    )


@lru_cache(maxsize=1)
def words() -> dict[str, dict[str, list[str]]]:
    """{folded dictionary key: {darija: [...], syn: [...]}} from the referential file."""
    try:
        raw = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {_key(k): v for k, v in raw.items() if isinstance(v, dict)}


def _names(ing: Ingredient | ru.Ing) -> list[str]:
    """Every way to say an ingredient: French, Arabic (each « / » variant), darija, synonyms."""
    extra = words().get(_key(ing.key), {})
    out = [ing.name_fr, ing.key, *str(ing.name_ar or "").replace("(", "/").replace(")", "/").split("/")]
    out += list(extra.get("darija") or []) + list(extra.get("syn") or [])
    return [n for n in (_key(x) for x in out) if n]


@dataclass
class Dictionary:
    """The ingredients as the matching sees them."""

    ids: dict[int, Ingredient] = field(default_factory=dict)
    by_key: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)
    by_singular: dict[str, int] = field(default_factory=dict)
    family: dict[int, frozenset[int]] = field(default_factory=dict)

    @classmethod
    def build(cls, rows: list[Ingredient]) -> Dictionary:
        d = cls()
        for row in sorted(rows, key=lambda r: r.id):
            d.ids[row.id] = row
            d.by_key[_key(row.key)] = row.id
        for row in sorted(rows, key=lambda r: r.id):  # an exact name wins over a synonym: names first
            for name in (_key(row.name_fr), _key(row.key)):
                d.by_name.setdefault(name, row.id)
        for row in sorted(rows, key=lambda r: r.id):
            for name in _names(row):
                d.by_name.setdefault(name, row.id)
        for name, iid in d.by_name.items():
            d.by_singular.setdefault(singular(name), iid)
        for fam in FAMILIES:
            members = frozenset(d.by_key[k] for k in fam if k in d.by_key)
            for m in members:
                d.family[m] = members
        return d

    def resolve(self, value: Any) -> int | None:
        """An id (number) or a word (« tmatem », « بصل », « Pois chiches ») → the dictionary id."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            n = int(value)
            return n if n in self.ids else None
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            return None
        k = _key(value)
        if k in self.by_name:
            return self.by_name[k]
        if k.endswith("s") and k[:-1] in self.by_name:  # « tomates », « oignons »
            return self.by_name[k[:-1]]
        return self.by_singular.get(singular(k))  # « pommes de terre », « haricot vert »

    def widen(self, ids: set[int]) -> set[int]:
        out = set(ids)
        for i in ids:
            out |= self.family.get(i, frozenset())
        return out


async def dictionary(session: AsyncSession) -> Dictionary:
    return Dictionary.build(list((await session.execute(select(Ingredient))).scalars()))


@dataclass
class Match:
    recipe: Recipe
    have_n: int
    missing: list[ru.Ing]
    present_buy_ids: set[int | str]


def match(
    data: recipes.Loaded,
    have: set[int],
    *,
    pantry_off: set[int] | frozenset[int] = frozenset(),
    use_pantry: bool = True,
    max_missing: int = MAX_MISSING,
) -> list[Match]:
    """The rules (module docstring), on loaded recipes. `have` = the customer's ids, families widened."""
    out: list[Match] = []
    for recipe in data.recipes:
        mine: set[int] = set()
        missing: dict[int | str, ru.Ing] = {}
        present: set[int | str] = set()
        for line in data.lines.get(recipe.id, []):
            ing, _ = recipes.line_need(data, line, ru.scale(line.qty, 1, recipe.base_servings))
            iid = line.ingredient_id if line.ingredient_id is not None else ing.id
            is_placard = bool(line.pantry or ing.pantry)
            if iid in have and not is_placard:
                mine.add(iid)
                present.add(ing.id)
            elif iid in have or line.optional or (is_placard and use_pantry and iid not in pantry_off):
                present.add(ing.id)
            else:
                missing.setdefault(iid, ing)
        present -= {ing.id for ing in missing.values()}  # also on an optional line: still to buy
        if mine and len(missing) <= max_missing:
            out.append(Match(recipe, len(mine), list(missing.values()), present))
    out.sort(
        key=lambda m: (
            len(m.missing),
            -m.have_n,
            m.recipe.time_min if m.recipe.time_min else 10_000,
            m.recipe.title_fr,
        )
    )
    return out


def _ids(raw: Any, limit: int = MAX_HAVE) -> list[Any]:
    return raw[:limit] if isinstance(raw, list) else []


async def match_recipes(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{have: [id | word], pantry_off?: [id], use_pantry? (true), cuisine?, country?, category?, tag?,
    max_time?, q?, per_group? (≤ 60)} → {have: [ids], unknown: [words], groups: [{missing, total, recipes}],
    total}."""
    await recipes._require_see(session, viewer)
    dico = await dictionary(session)
    have: set[int] = set()
    unknown: list[str] = []
    for v in _ids(payload.get("have")):
        iid = dico.resolve(v)
        if iid is None:
            if isinstance(v, str) and v.strip():
                unknown.append(v.strip()[:80])
        else:
            have.add(iid)
    pantry_off = {i for i in (dico.resolve(v) for v in _ids(payload.get("pantry_off"), 300)) if i is not None}
    use_pantry = payload.get("use_pantry") is not False
    try:
        per_group = max(1, min(PER_GROUP_MAX, int(payload.get("per_group") or PER_GROUP)))
    except (TypeError, ValueError):
        per_group = PER_GROUP
    groups: list[dict[str, Any]] = [{"missing": n, "total": 0, "recipes": []} for n in range(MAX_MISSING + 1)]
    answer: dict[str, Any] = {
        "success": True,
        "have": sorted(have),
        "unknown": unknown,
        "groups": groups,
        "total": 0,
    }
    if not have:
        return answer
    filters = {k: payload.get(k) for k in ("cuisine", "country", "category", "tag", "max_time", "q")}
    ids = list((await session.execute(select(Recipe.id).where(*recipes._filters(filters)))).scalars())
    data = await recipes.load(session, ids=ids, published_only=False)
    found = match(data, dico.widen(have), pantry_off=pantry_off, use_pantry=use_pantry)
    prices = await recipe_prices.prices(session) if found else None
    for m in found:
        g = groups[len(m.missing)]
        g["total"] += 1
        if len(g["recipes"]) >= per_group:
            continue
        n = m.recipe.base_servings
        rid: uuid.UUID = m.recipe.id
        card = recipes._card(
            m.recipe, recipes._card_cost(data, m.recipe, n, prices), len(data.lines.get(rid, [])), n
        )
        rest = recipes.shopping(data, [(rid, n)], m.present_buy_ids)
        card.update(
            have_n=m.have_n,
            missing=[{"id": i.id, "name_fr": i.name_fr, "name_ar": i.name_ar} for i in m.missing],
            missing_cost=recipes._cost_doc(recipe_prices.cost(rest["buy"], prices)) if m.missing else None,
            at_home=sorted(m.present_buy_ids, key=str),
        )
        g["recipes"].append(card)
    answer["total"] = sum(g["total"] for g in groups)
    return answer


async def list_ingredients(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{} → the dictionary for the « j'ai » search on the phone: [{id, key, name_fr, name_ar, darija, syn,
    pantry, aisle, uses}] (uses = recipes that need it), the quick picks, the placard ids, the families."""
    await recipes._require_see(session, viewer)
    dico = await dictionary(session)
    uses: dict[int, int] = {}
    rows = await session.execute(
        select(RecipeLine.ingredient_id, RecipeLine.recipe_id)
        .join(Recipe, Recipe.id == RecipeLine.recipe_id)
        .where(Recipe.status.in_(recipes.BROWSABLE), RecipeLine.ingredient_id.is_not(None))
        .distinct()
    )
    for iid, _ in rows.all():
        uses[iid] = uses.get(iid, 0) + 1
    out = []
    for iid, row in sorted(dico.ids.items()):
        extra = words().get(_key(row.key), {})
        out.append(
            {
                "id": iid,
                "key": row.key,
                "name_fr": row.name_fr,
                "name_ar": row.name_ar,
                "darija": list(extra.get("darija") or []),
                "syn": list(extra.get("syn") or []),
                "pantry": row.pantry,
                "aisle": row.aisle,
                "uses": uses.get(iid, 0),
            }
        )
    return {
        "success": True,
        "ingredients": out,
        "quick": [dico.by_key[k] for k in QUICK if k in dico.by_key],
        "placard": [i["id"] for i in out if i["pantry"]],
        "families": sorted({tuple(sorted(f)) for f in dico.family.values()}),
    }
