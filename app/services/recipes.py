"""Recettes (21/10/2026, first version): « je choisis une recette, je dis pour combien de personnes, je coche
ce que j'ai déjà, et un livreur m'apporte le reste ».

Spec: ods-audit/recettes-delivery-2110/ETUDE.md (T2, T4) and PRIX-INGREDIENTS.md (honest cost). The order a
recipe list makes is a NORMAL Achats order (placeOrder → offers, « Article indisponible », Offre Chaude…,
nothing changed): `items_text` = one line per article the courier ticks, rebuilt here server side from the
recipes, the people and the « j'ai déjà » — never the client's text.

Visibility (owner 21/10, evening: « pour le moment les Recettes uniquement côté ADMIN »), enforced HERE:
ADMINS ONLY. Every client route (listRecipes, getRecipe, previewRecipeList, placeOrder with `recipes`) answers
403 to anybody else — customers, couriers AND the test accounts (QA_ACCOUNTS) — whatever the switch « Recettes
publiques » (`app_settings['recipes'].public_allowed`, off) says: opening to the public later needs a code
change here (ADMIN_ONLY) on top of that switch. An order from « Ma liste » is allowed only to an admin who
uses a test account (QA_ACCOUNTS) and always carries « QA TEST »: offered to the QA couriers only (existing
dispatch rule); any other admin is refused (`recipes_order_disabled`).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import (
    AppSetting,
    AuditLog,
    Ingredient,
    Place,
    Recipe,
    RecipeLine,
    RecipeOrder,
    RecipePriceCache,
    User,
)
from app.security.deps import CurrentUser
from app.services import recipe_photos, recipe_prices, recipe_videos, text_norm
from app.services import recipe_units as ru
from app.services.orders import MAX_BUDGET, OrderRefused, is_qa_account
from app.services.places import lat_expr, lng_expr, point

SETTINGS_KEY = "recipes"
PUBLIC_ALLOWED = "public_allowed"
ADMIN_ONLY = True  # owner 21/10: the Recettes are an admin screen for now (customers, test accounts: 403)
LABEL = "[Recettes]"
TEST_PREFIX = "QA TEST "
MAX_RECIPES = 8  # recipes in one list
MAX_SERVINGS = 100  # people, or pieces for a pastry made by 30
ITEMS_TEXT_MAX = 2000
NOTES_MAX = 500
NEAREST_SHOP_KM = 5.0
SHOP_CATEGORY = "supermarché"
AISLE_ORDER = ("marche", "boucherie", "poissonnerie", "cremerie", "boulangerie", "epicerie")
POLICIES = ("call_me", "substitute", "skip", "cancel")
TEXTS = {
    "fr": {
        "people": "pers.",
        "pieces": "pièces",
        "portions": "portions",
        "nearest": "Supermarché ou épicerie le plus proche",
        "nearest_note": "Courses de recettes : magasin le plus proche, au choix du livreur.",
        "unpriced_note": "Sans prix connu (le client accepte le prix du ticket) : {names}.",
    },
    "ar": {
        "people": "أشخاص",
        "pieces": "قطعة",
        "portions": "حصص",
        "nearest": "أقرب مغازة أو عطّار",
        "nearest_note": "مشتريات وصفات: أقرب محل، يختاره المندوب.",
        "unpriced_note": "بدون سعر معروف (الحريف يقبل سعر الوصل): {names}.",
    },
}


# Arabic number + noun agreed (QA 22/10: « 30 قطع », « 12 حصص »): one, two (alone), 3–10, 11 and more
AR_SERVINGS = {
    "people": ("شخص", "شخصان", "أشخاص", "شخصاً"),
    "pieces": ("قطعة", "قطعتان", "قطع", "قطعة"),
    "portions": ("حصة", "حصتان", "حصص", "حصة"),
}


def servings_text(n: int, unit: str, lang: str) -> str:
    """« 4 pers. », « 30 pièces » / « 4 أشخاص », « قطعتان », « 30 قطعة », « 12 شخصاً »."""
    if lang != "ar":
        return f"{n} {TEXTS['fr'].get(unit, TEXTS['fr']['people'])}"
    one, two, few, many = AR_SERVINGS.get(unit, AR_SERVINGS["people"])
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {few}"
    return f"{n} {many if n >= 11 else one}"


def _lang(value: Any) -> str:
    return "ar" if value == "ar" else "fr"


def _admin(user: CurrentUser | None) -> None:
    if user is None or not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")


def _invalid(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


def _iso(value: datetime | None) -> str | None:
    return (
        None if value is None else value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


# --- settings and visibility ---------------------------------------------------------------------------


async def public_allowed(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return value.get(PUBLIC_ALLOWED) is True


async def can_see(session: AsyncSession, viewer: CurrentUser | None) -> bool:
    if viewer is None:
        return False
    if viewer.is_admin:
        return True
    if ADMIN_ONLY:
        return False
    return is_qa_account(viewer.email) or await public_allowed(session)


async def _require_see(session: AsyncSession, viewer: CurrentUser | None) -> None:
    if not await can_see(session, viewer):
        raise ApiError(403, "forbidden", "Admin only")


async def get_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    return {"success": True, PUBLIC_ALLOWED: await public_allowed(session)}


async def set_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    wanted = payload.get(PUBLIC_ALLOWED)
    if not isinstance(wanted, bool):
        raise _invalid(f"{PUBLIC_ALLOWED}: true or false")
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    before = value.get(PUBLIC_ALLOWED) is True
    value[PUBLIC_ALLOWED] = wanted
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=value, updated_by=admin.id))
    else:
        row.value, row.updated_by = value, admin.id
    session.add(
        AuditLog(
            actor_user_id=admin.id,
            action="recipes.settings",
            entity="app_settings",
            entity_id=SETTINGS_KEY,
            before={PUBLIC_ALLOWED: before},
            after={PUBLIC_ALLOWED: wanted},
        )
    )
    await session.flush()
    return {"success": True, PUBLIC_ALLOWED: wanted}


# --- loading -------------------------------------------------------------------------------------------


def to_ing(row: Ingredient) -> ru.Ing:
    return ru.Ing(
        id=row.id,
        key=row.key,
        name_fr=row.name_fr,
        name_ar=row.name_ar,
        unit=row.unit,
        aisle=row.aisle,
        pantry=row.pantry,
        piece_kg=row.piece_kg,
        density=row.density,
    )


class Loaded:
    """Recipes with their lines and ingredients (3 queries)."""

    def __init__(
        self, recipes: list[Recipe], lines: dict[uuid.UUID, list[RecipeLine]], ings: dict[int, ru.Ing]
    ):
        self.recipes = recipes
        self.lines = lines
        self.ings = ings
        self.by_id = {r.id: r for r in recipes}


async def load(
    session: AsyncSession, *, ids: list[uuid.UUID] | None = None, published_only: bool = True
) -> Loaded:
    stmt = select(Recipe)
    if ids is not None:
        stmt = stmt.where(Recipe.id.in_(ids))
    if published_only:
        stmt = stmt.where(Recipe.status == "published")
    recipes = list((await session.execute(stmt.order_by(Recipe.title_fr))).scalars())
    lines: dict[uuid.UUID, list[RecipeLine]] = defaultdict(list)
    ings: dict[int, ru.Ing] = {}
    if recipes:
        rows = (
            await session.execute(
                select(RecipeLine)
                .where(RecipeLine.recipe_id.in_([r.id for r in recipes]))
                .order_by(RecipeLine.recipe_id, RecipeLine.position)
            )
        ).scalars()
        for line in rows:
            lines[line.recipe_id].append(line)
        wanted = {
            line.ingredient_id for ls in lines.values() for line in ls if line.ingredient_id is not None
        }
        if wanted:
            for ing in (await session.execute(select(Ingredient).where(Ingredient.id.in_(wanted)))).scalars():
                ings[ing.id] = to_ing(ing)
    return Loaded(recipes, lines, ings)


# --- the shopping list ---------------------------------------------------------------------------------


def line_need(data: Loaded, line: RecipeLine, qty: Decimal) -> tuple[ru.Ing, Decimal]:
    """The ingredient a line buys and how much, in its buying unit (unrounded).

    A line without a dictionary reference (`ingredient_new`) or whose unit cannot be turned into the
    dictionary's without guessing stays on the list with its own names and unit, WITHOUT a price (its id
    never matches a price): never blocking."""
    ing = data.ings.get(line.ingredient_id) if line.ingredient_id is not None else None
    if ing is not None:
        q = ru.to_buy_unit(qty, line.unit, ing)
        if q is not None:
            return ing, q
        unit = line.unit if line.unit in ru.UNIT_WORDS["fr"] and line.unit not in ru.SPOON else "kg"
        return replace(ing, id=f"x:{ing.id}:{unit}", key=f"x:{ing.id}:{unit}", unit=unit, piece_kg=None), qty
    key = f"n:{line.new_key or line.name_fr}"
    unit = line.unit if line.unit in ("kg", "l", "piece", "botte") else "kg"
    loose = ru.Ing(
        id=key,
        key=key,
        name_fr=line.name_fr,
        name_ar=line.name_ar,
        unit=unit,
        aisle=line.aisle or "epicerie",
        pantry=line.pantry,
        piece_kg=line.piece_kg,
    )
    if unit == line.unit:
        return loose, qty
    return loose, ru.to_buy_unit(qty, line.unit, loose) or qty


OptionalTicks = dict[uuid.UUID, set[int | str]] | set[int | str] | None


def _opted(with_optional: OptionalTicks, recipe_id: uuid.UUID) -> set[int | str]:
    """The optional articles the customer ticked for this recipe (a set = the same for every recipe)."""
    if with_optional is None:
        return set()
    if isinstance(with_optional, dict):
        return with_optional.get(recipe_id) or set()
    return with_optional


def needs(
    data: Loaded, items: list[tuple[uuid.UUID, int]], with_optional: OptionalTicks = None
) -> list[tuple[ru.Ing, Decimal]]:
    """What the recipes × people need, in buying units, summed by ingredient (not rounded yet).

    QA 22/10 (owner: « facultatif = décoché par défaut, ni acheté ni compté »): a line the recipe marks
    optional is bought only when the customer ticked it (`with_optional`, per recipe)."""
    out: list[tuple[ru.Ing, Decimal]] = []
    for recipe_id, servings in items:
        recipe = data.by_id[recipe_id]
        opted = _opted(with_optional, recipe_id)
        for line in data.lines.get(recipe_id, []):
            ing, q = line_need(data, line, ru.scale(line.qty, servings, recipe.base_servings))
            if line.optional and ing.id not in opted:
                continue
            if q > 0:
                out.append((ing, q))
    return ru.merge(out)


def optional_left(
    data: Loaded, items: list[tuple[uuid.UUID, int]], with_optional: OptionalTicks, bought: set[int | str]
) -> list[dict[str, Any]]:
    """The optional articles NOT bought (« facultatif », to tick): one per ingredient, with its recipes."""
    out: dict[int | str, dict[str, Any]] = {}
    for recipe_id, servings in items:
        recipe = data.by_id[recipe_id]
        opted = _opted(with_optional, recipe_id)
        for line in data.lines.get(recipe_id, []):
            if not line.optional:
                continue
            ing, q = line_need(data, line, ru.scale(line.qty, servings, recipe.base_servings))
            if ing.id in opted or ing.id in bought or q <= 0:
                continue
            doc = out.setdefault(
                ing.id,
                {
                    "ingredient_id": ing.id,
                    "name_fr": ing.name_fr,
                    "name_ar": ing.name_ar,
                    "aisle": ing.aisle,
                    "unit": ing.unit,
                    "qty": 0.0,
                    "recipe_ids": [],
                },
            )
            doc["qty"] = float(Decimal(str(doc["qty"])) + q)
            if str(recipe_id) not in doc["recipe_ids"]:
                doc["recipe_ids"].append(str(recipe_id))
    for doc in out.values():
        q = ru.round_buy(Decimal(str(doc["qty"])), doc["unit"])
        doc["qty"] = float(q)
        doc["text_fr"] = ru.fmt_qty(q, doc["unit"], "fr")
        doc["text_ar"] = ru.fmt_qty(q, doc["unit"], "ar")
    return list(out.values())


def shopping(
    data: Loaded, items: list[tuple[uuid.UUID, int]], at_home: set[int], with_optional: OptionalTicks = None
) -> dict[str, Any]:
    """The list to buy (rounded, by aisle), what is at home, the weight (optional lines only if ticked)."""
    merged = needs(data, items, with_optional)
    buy: list[tuple[ru.Ing, Decimal]] = []
    home: list[ru.Ing] = []
    for ing, q in merged:
        if ing.id in at_home:
            home.append(ing)
        else:
            buy.append((ing, ru.round_buy(q, ing.unit)))
    order = {a: i for i, a in enumerate(AISLE_ORDER)}
    buy.sort(key=lambda t: (order.get(t[0].aisle, 99), t[0].name_fr))
    weight = Decimal(0)
    unknown_weight = 0
    for ing, q in buy:
        w = ru.weight_kg(q, ing.unit, ing)
        if w is None:
            unknown_weight += 1
        else:
            weight += w
    return {"buy": buy, "home": home, "weight_kg": weight, "unknown_weight": unknown_weight}


def _line_doc(ing: ru.Ing, q: Decimal, cost: dict[str, Any] | None) -> dict[str, Any]:
    doc = {
        "ingredient_id": ing.id,
        "key": ing.key,
        "name_fr": ing.name_fr,
        "name_ar": ing.name_ar,
        "aisle": ing.aisle,
        "qty": float(q),
        "unit": ing.unit,
        "text_fr": ru.fmt_qty(q, ing.unit, "fr"),
        "text_ar": ru.fmt_qty(q, ing.unit, "ar"),
    }
    if cost is not None:
        doc["price"] = cost
    return doc


def _cost_doc(c: dict[str, Any]) -> dict[str, Any]:
    """What the screens need of a cost (no per-line detail)."""
    if not c.get("available"):
        return {"available": False}
    return {k: v for k, v in c.items() if k != "lines"}


def _pantry_defaults(data: Loaded, recipe_ids: list[uuid.UUID]) -> set[int | str]:
    """What is ticked « j'ai déjà » at first: the lines the recipe marks « placard » (salt, oil, spices…)."""
    out: set[int | str] = set()
    for rid in recipe_ids:
        recipe = data.by_id[rid]
        for line in data.lines.get(rid, []):
            if line.pantry:
                out.add(line_need(data, line, ru.scale(line.qty, 1, recipe.base_servings))[0].id)
    return out


# --- client reads --------------------------------------------------------------------------------------


def _card(recipe: Recipe, c: dict[str, Any], lines_n: int, servings: int | None = None) -> dict[str, Any]:
    """A recipe card. The cost is for `servings` (the recipe's own number unless the list asked another)."""
    n = servings or recipe.base_servings
    return {
        "id": str(recipe.id),
        "slug": recipe.slug,
        "title_fr": recipe.title_fr,
        "title_ar": recipe.title_ar,
        "title_local": recipe.title_local,
        "cuisine": recipe.cuisine,
        "country": recipe.country,
        "category": recipe.category,
        "time_min": recipe.time_min,
        "prep_min": recipe.prep_min,
        "seasons": list(recipe.seasons or []),
        "cook_min": recipe.cook_min,
        "difficulty": recipe.difficulty,
        "base_servings": recipe.base_servings,
        "servings_unit": recipe.servings_unit,
        "tags": list(recipe.tags or []),
        "ingredients_count": lines_n,
        "cost": _cost_doc(c),
        "cost_servings": n,
        "per_person": recipe_prices.per_person(c, n),
        **recipe_photos.fields(recipe),
    }


PAGE_SIZE = 20
# while Recettes is admins-only; back to ("published",) before any opening
BROWSABLE = ("published", "draft")
PAGE_SIZE_MAX = 40
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})


_LIGATURES = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae"})


def fold(*values: Any) -> str:
    """Titles / a query folded the same way: no case, no accent, Arabic letters unified, « œ » = « oe »
    (QA 22/10: « boeuf », « oeufs » found nothing — NFD does not split the ligature; the phone keyboards have
    no « œ »). Done here and not in text_norm: the places' stored search_norm would no longer match."""
    return text_norm.normalize_text(*(str(v).translate(_LIGATURES) for v in values)).translate(_AR_FOLD)


# Usual spellings of the same word (QA 22/10, owner n° 17): a query word equal to one of them — or the start
# of one, from 4 letters — also looks for the others. Folded forms (fold above). Synonyms only, no fuzzy
# search.
SEARCH_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("tagine", "tajine", "tajin", "طاجين", "طجين"),
    ("couscous", "kousksi", "kosksi", "kouskous", "kesksou", "كسكسي", "كسكس"),
    ("mloukhia", "mlokhia", "molokhia", "mloukhiya", "molokheya", "mouloukhia", "ملوخيه"),
    ("chakchouka", "shakshuka", "shakshouka", "chakchuka", "شكشوكه"),
    ("harissa", "hrissa", "هريسه"),
    ("dajaj", "djej", "djaj", "poulet", "دجاج"),
    ("maadnous", "ma9dnous", "ma3dnous", "persil", "بقدونس", "معدنوس"),
    ("batata", "بطاطا", "بطاطس"),
    ("merguez", "mirgaz", "mergaz", "مرقاز"),
)
_SYNONYM_OF: dict[str, tuple[str, ...]] = {w: group for group in SEARCH_SYNONYMS for w in group}


def search_words(token: str) -> list[str]:
    """The folded query word and its usual spellings (« tajine » → tajine, tagine, طاجين…)."""
    words = [token]
    groups = {_SYNONYM_OF[token]} if token in _SYNONYM_OF else set()
    if len(token) >= 4:
        groups |= {g for w, g in _SYNONYM_OF.items() if w.startswith(token)}
    for group in groups:
        words += [w for w in group if w not in words]
    return words


def _filters(payload: dict[str, Any]) -> list[Any]:
    # Admins only for now (owner 21/10): they browse the drafts too, to read the recipes before review.
    where: list[Any] = [Recipe.status.in_(BROWSABLE)]
    for field, column in (
        ("cuisine", Recipe.cuisine),
        ("country", Recipe.country),
        ("category", Recipe.category),
    ):
        raw = payload.get(field)
        plural = payload.get({"cuisine": "cuisines", "country": "countries", "category": "categories"}[field])
        if raw in (None, "", "all") and plural:  # the home's `cuisines=a,b` / `categories=[…]`
            raw = plural.split(",") if isinstance(plural, str) else plural
        if isinstance(raw, list):  # a region: several cuisines (« Égypte & Golfe »), soups and stews…
            values = [str(v).strip()[:60] for v in raw[:30] if str(v).strip()]
            if values:
                where.append(column.in_(values))
            continue
        value = str(raw or "").strip()
        if value and value != "all":
            where.append(column == value[:60])
    tag = str(payload.get("tag") or "").strip()
    if tag:  # « Petit budget », « Végétarien », « Ramadan »… whatever the case the files wrote them in
        where.append(
            text("lower(:recipe_tag) IN (SELECT lower(x) FROM unnest(recipes.tags) AS x)").bindparams(
                recipe_tag=tag[:30]
            )
        )
    season = str(payload.get("season") or "").strip().lower().replace("é", "e")
    if season:  # hiver / printemps / ete / automne / ramadan / aid (the home's « En ce moment »)
        where.append(Recipe.seasons.contains([season[:20]]))
    slugs = payload.get("slugs")
    if isinstance(slugs, list):  # « Vos recettes »: the favourites / last opened kept on the phone
        where.append(Recipe.slug.in_([str(x)[:120] for x in slugs[:40]]))
    try:
        max_time = int(payload.get("max_time") or 0)
    except (TypeError, ValueError):
        max_time = 0
    if max_time > 0:
        where.append(Recipe.time_min <= max_time)
    for token in fold(str(payload.get("q") or "")[:80]).split()[:6]:
        likes = []
        for word in search_words(token):
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            likes.append(Recipe.search_norm.like(f"%{escaped}%"))
        where.append(or_(*likes) if len(likes) > 1 else likes[0])
    return where


async def _facets(
    session: AsyncSession, payload: dict[str, Any] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The values of cuisine / country / category among the browsable recipes, with their counts.

    `payload` (`facets_scope: "selection"`, the Explorer's filters sheet, QA 22/10 r1-11): counted IN the
    current selection (search, group, tag, time…), each facet without its own filter (so another cuisine can
    still be chosen) — never a category that gives « Rien pour ce choix »."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name, column, own in (
        ("cuisines", Recipe.cuisine, ("cuisine", "cuisines")),
        ("countries", Recipe.country, ("country", "countries")),
        ("categories", Recipe.category, ("category", "categories")),
    ):
        where = (
            [Recipe.status.in_(BROWSABLE)]
            if payload is None
            else _filters({k: v for k, v in payload.items() if k not in own})
        )
        rows = (
            await session.execute(
                select(column, func.count())
                .where(*where, column.is_not(None))
                .group_by(column)
                .order_by(func.count().desc(), column)
            )
        ).all()
        out[name] = [{"key": key, "count": n} for key, n in rows]
    return out


def _people(recipe: Recipe, servings: int | None) -> int:
    """The number a card's cost is for: the asked one for a dish (people), the recipe's own otherwise."""
    return servings if servings and recipe.servings_unit == "people" else recipe.base_servings


def _card_cost(data: Loaded, recipe: Recipe, n: int, prices: Any) -> dict[str, Any]:
    lst = shopping(data, [(recipe.id, n)], _pantry_defaults(data, [recipe.id]))
    return recipe_prices.cost(lst["buy"], prices)


MAX_BUDGET_SCAN = (
    600  # « Ce soir avec 20 DT »: the costs of every matching recipe are computed (547 on 21/10)
)


async def list_recipes(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{q?, cuisine? (one or a list), country?, category? (one or a list), tag?, slugs?, max_time?, servings?,
    max_cost?, page? (1…), page_size? (≤ 40)} → one page of cards, the total, the facets (on page 1 only).

    `servings`: the costs are for that many people (dishes only; a pastry keeps its pieces). `priced_only`:
    the same rule as `max_cost` without a ceiling. `cuisines` / `categories` (a list or « a,b »), `season`
    (hiver / printemps / ete / automne / ramadan / aid), `tag` (any case). `max_cost`
    (« Ce soir avec 20 DT pour 4 »): only the recipes whose WHOLE cost is known (a total shown, ≥ 70 %
    priced) and whose high end fits, Tunisian first then the cheapest; never a recipe without a total."""
    await _require_see(session, viewer)
    try:
        page = max(1, int(payload.get("page") or 1))
        size = max(1, min(PAGE_SIZE_MAX, int(payload.get("page_size") or PAGE_SIZE)))
    except (TypeError, ValueError):
        page, size = 1, PAGE_SIZE
    servings = _servings(payload.get("servings"), 0) if payload.get("servings") is not None else None
    try:
        max_cost = (
            Decimal(str(payload.get("max_cost"))) if payload.get("max_cost") not in (None, "") else None
        )
    except ArithmeticError:
        max_cost = None
    if max_cost is not None and (not max_cost.is_finite() or max_cost <= 0):
        max_cost = None
    where = _filters(payload)
    priced_only = str(payload.get("priced_only") or "").lower() in ("1", "true", "yes")
    if max_cost is not None or priced_only:  # only recipes whose whole cost is shown (≥ 70 % priced)
        servings = servings or 4
        ids = list((await session.execute(select(Recipe.id).where(*where).limit(MAX_BUDGET_SCAN))).scalars())
        data = await load(session, ids=ids, published_only=False)
        prices = await recipe_prices.prices(session) if ids else None
        fitting = []
        for recipe in data.recipes:
            if recipe.servings_unit != "people":
                continue
            c = _card_cost(data, recipe, servings, prices)
            if (
                c.get("show_total")
                and c.get("high") is not None
                and (max_cost is None or Decimal(str(c["high"])) <= max_cost)
            ):
                fitting.append((recipe, c))
        fitting.sort(key=lambda t: (t[0].cuisine != "tunisienne", float(t[1]["high"]), t[0].title_fr))
        total = len(fitting)
        cards = [
            _card(recipe, c, len(data.lines.get(recipe.id, [])), servings)
            for recipe, c in fitting[(page - 1) * size : page * size]
        ]
    else:
        total = (await session.execute(select(func.count()).select_from(Recipe).where(*where))).scalar_one()
        ids = list(
            (
                await session.execute(
                    select(Recipe.id)
                    .where(*where)
                    .order_by(Recipe.title_fr, Recipe.id)
                    .offset((page - 1) * size)
                    .limit(size)
                )
            ).scalars()
        )
        data = await load(session, ids=ids, published_only=False)
        prices = await recipe_prices.prices(session) if ids else None
        cards = []
        for recipe in data.recipes:
            n = _people(recipe, servings)
            cards.append(
                _card(recipe, _card_cost(data, recipe, n, prices), len(data.lines.get(recipe.id, [])), n)
            )
    answer: dict[str, Any] = {
        "success": True,
        "visible": True,
        "test": not await public_allowed(session),
        "recipes": cards,
        "total": total,
        "page": page,
        "page_size": size,
        "has_more": page * size < total,
    }
    if page == 1:
        scoped = str(payload.get("facets_scope") or "") == "selection"
        answer["facets"] = await _facets(session, payload if scoped else None)
    return answer


async def _visible_recipe(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> Recipe:
    await _require_see(session, viewer)
    rid = _uuid(payload.get("id"))
    slug = str(payload.get("slug") or "").strip()
    stmt = (
        select(Recipe).where(Recipe.id == rid)
        if rid is not None
        else select(Recipe).where(Recipe.slug == slug)
    )
    recipe = (await session.execute(stmt)).scalar_one_or_none() if (rid or slug) else None
    admin = viewer is not None and viewer.is_admin
    if recipe is None or (recipe.status != "published" and not admin):
        raise ApiError(404, "not_found", "Recipe not found")
    return recipe


def _servings(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_SERVINGS, n))


def _at_home(value: Any, default: set[int | str]) -> set[int | str]:
    """The ids ticked « j'ai déjà »: dictionary ids (numbers) or the « n:… » / « x:… » keys of the others."""
    if not isinstance(value, list):
        return default
    out: set[int | str] = set()
    for v in value[:300]:
        if isinstance(v, bool):
            continue
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            out.add(int(v))
        elif isinstance(v, str) and v[:2] in ("n:", "x:") and len(v) <= 120:
            out.add(v)
    return out


async def get_recipe(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{id | slug, servings?, at_home?, with_optional?} → the recipe: exact quantities to cook for `servings`,
    the list to buy (rounded) without what is at home (pantry pre-ticked unless `at_home` is sent), its honest
    cost. An optional line (« facultatif ») is bought only when its id is in `with_optional` (QA 22/10)."""
    recipe = await _visible_recipe(session, viewer, payload)
    data = await load(session, ids=[recipe.id], published_only=False)
    servings = _servings(payload.get("servings"), recipe.base_servings)
    pantry = _pantry_defaults(data, [recipe.id])
    at_home = _at_home(payload.get("at_home"), pantry)
    opted = _at_home(payload.get("with_optional"), set())
    lst = shopping(data, [(recipe.id, servings)], at_home, opted)
    prices = await recipe_prices.prices(session)
    c = recipe_prices.cost(lst["buy"], prices)
    buy = {ing.id: q for ing, q in lst["buy"]}
    ingredients = []
    for line in data.lines.get(recipe.id, []):
        exact = ru.scale(line.qty, servings, recipe.base_servings)
        ing = line_need(data, line, exact)[0]
        ingredients.append(
            {
                "ingredient_id": ing.id,
                "name_fr": line.name_fr,
                "name_ar": line.name_ar,
                "aisle": ing.aisle,
                "pantry": line.pantry,
                "referenced": not isinstance(ing.id, str),
                "at_home": ing.id in at_home,
                "optional": line.optional,
                "wanted": (not line.optional) or ing.id in opted,
                "note_fr": line.note_fr,
                "note_ar": line.note_ar,
                "qty": float(exact),
                "unit": line.unit,
                "text_fr": ru.fmt_qty(exact, line.unit, "fr"),
                "text_ar": ru.fmt_qty(exact, line.unit, "ar"),
                "buy": _line_doc(ing, buy[ing.id], c.get("lines", {}).get(ing.id)) if ing.id in buy else None,
            }
        )
    return {
        "success": True,
        "recipe": {
            **_card(recipe, c, len(ingredients), servings),
            "status": recipe.status,
            "intro_fr": recipe.intro_fr,
            "intro_ar": recipe.intro_ar,
            "tip_fr": recipe.tip_fr,
            "tip_ar": recipe.tip_ar,
            "steps_fr": list(recipe.steps_fr or []),
            "steps_ar": list(recipe.steps_ar or []),
            # 22/10: per step, its YouTube passage and the gestures it uses (null when none)
            "step_media": await recipe_videos.step_media(session, recipe),
            "servings": servings,
            "ingredients": ingredients,
            "weight_kg": float(lst["weight_kg"]),
            "cost": _cost_doc(c),
        },
    }


async def _items(
    session: AsyncSession, viewer: CurrentUser | None, raw: Any
) -> tuple[Loaded, list[tuple[uuid.UUID, int]]]:
    if not await can_see(session, viewer):
        raise OrderRefused(403, "forbidden")
    if not isinstance(raw, list) or not raw:
        raise OrderRefused(400, "recipes_empty")
    wanted: dict[uuid.UUID, int] = {}
    for item in raw[: MAX_RECIPES * 2]:
        rid = _uuid(item.get("recipe_id") if isinstance(item, dict) else None)
        if rid is None:
            raise OrderRefused(400, "recipes_unavailable")
        wanted[rid] = min(MAX_SERVINGS, wanted.get(rid, 0) + _servings(item.get("servings"), 4))
    if len(wanted) > MAX_RECIPES:
        raise OrderRefused(400, "too_many_recipes", max=MAX_RECIPES)
    # while Recettes is admins-only (owner 21/10) « Ma liste » and the order take the drafts too, like the
    # browsing (BROWSABLE); back to published only before any opening
    data = await load(session, ids=list(wanted), published_only=False)
    data_ok = {rid for rid, r in data.by_id.items() if r.status in BROWSABLE}
    missing = [str(rid) for rid in wanted if rid not in data_ok]
    if missing:
        raise OrderRefused(409, "recipes_unavailable", ids=missing)
    return data, list(wanted.items())


MAX_OTHERS = 20  # « + Ajouter autre chose » (lait, pain, eau…): free lines, never priced
OTHER_MAX = 60


def _other_text(value: Any) -> str:
    """« Lait » / {name: « Lait », count: 2} (Ma liste A sends `extras` as objects) → « Lait × 2 »."""
    if isinstance(value, dict):
        name = " ".join(str(value.get("name") or "").split())[:OTHER_MAX]
        try:
            count = int(value.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        return f"{name} × {min(count, 99)}" if name and count > 1 else name
    return " ".join(str(value).split())[:OTHER_MAX] if isinstance(value, str | int | float) else ""


def _others(raw: Any, extras: Any = None) -> list[str]:
    """The customer's own extra articles (`others`: texts, or `extras`: [{name, count}]): trimmed, no
    duplicates, ≤ 12 of ≤ 60 characters (+ the count)."""
    out: list[str] = []
    values = [*(raw if isinstance(raw, list) else []), *(extras if isinstance(extras, list) else [])]
    for value in values[: MAX_OTHERS * 2]:
        text_ = _other_text(value)
        if text_ and text_.casefold() not in {o.casefold() for o in out}:
            out.append(text_)
    return out[:MAX_OTHERS]


def _item_optional(raw: Any) -> dict[uuid.UUID, set[int | str]]:
    """Per recipe of the list, the optional articles the customer ticked (`items[].with_optional`), plus a
    list-wide `with_optional` for every recipe (QA 22/10: « facultatif » = not bought unless ticked)."""
    out: dict[uuid.UUID, set[int | str]] = defaultdict(set)
    for item in (raw if isinstance(raw, list) else [])[: MAX_RECIPES * 2]:
        rid = _uuid(item.get("recipe_id") if isinstance(item, dict) else None)
        if rid is not None:
            out[rid] |= _at_home(item.get("with_optional"), set())
    return out


def _with_optional(r: dict[str, Any]) -> dict[uuid.UUID, set[int | str]]:
    per_item = _item_optional(r.get("items"))
    everywhere = _at_home(r.get("with_optional"), set())
    if everywhere:
        for rid in list(per_item):
            per_item[rid] |= everywhere
    return per_item


# « + Ajouter autre chose »: the weight of a typed article (QA 22/10 #4, the couffin and the parcel size),
# read from its words — the usual pack of the article; anything else counts a prudent flat weight.
OTHER_WEIGHTS: tuple[tuple[tuple[str, ...], Decimal], ...] = (
    (("pack d eau", "pack eau", "pack"), Decimal("9")),  # 6 × 1,5 L
    (("eau", "maa", "ماء", "مياه"), Decimal("1.5")),
    (("lait", "halib", "حليب"), Decimal("1.05")),
    (("jus", "soda", "boisson", "coca", "عصير", "مشروب", "gazouz", "قازوز"), Decimal("1.5")),
    (("huile", "zit", "زيت"), Decimal("1")),
    (
        ("sucre", "farine", "semoule", "riz", "pates", "سكر", "فارينة", "دقيق", "سميد", "أرز", "مقرونة"),
        Decimal("1"),
    ),
    (("yaourt", "yogourt", "ياغورت", "زبادي", "ياوورت"), Decimal("0.12")),
    (("baguette", "pain", "khobz", "خبز", "باقات"), Decimal("0.25")),
    (("oeuf", "oeufs", "بيض", "عظم"), Decimal("0.06")),
    (("beurre", "fromage", "زبدة", "جبن", "فرماج"), Decimal("0.25")),
)
OTHER_FLAT_KG = Decimal("1")  # an article whose weight is not known: 1 kg each, never 0


def _other_weight(text_: str) -> Decimal:
    """« Lait × 2 » → 2.1 kg, « eau 1,5 L x6 » → 9 kg, « Lessive » → 1 kg (the flat weight)."""
    raw = str(text_ or "")
    count = 1
    m = re.search(r"[×x]\s*(\d{1,2})\s*$", raw) or re.match(r"^\s*(\d{1,2})\s*[×x]?\s+\D", raw)
    if m:
        count = max(1, int(m.group(1)))
    words = f" {fold(raw)} "
    litres = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:l|litres?|لتر)(?![a-z])", raw.lower())
    for keys, kg in OTHER_WEIGHTS:
        if any(f" {fold(k)} " in words for k in keys):
            each = kg
            if litres and 1 <= kg < 5:  # « eau 5 L », « lait 0,5 L »; a pack keeps its 9 kg
                each = Decimal(litres.group(1).replace(",", "."))
            return each * count
    if litres:
        return Decimal(litres.group(1).replace(",", ".")) * count
    return OTHER_FLAT_KG * count


def others_weight(others: list[str]) -> Decimal:
    return sum((_other_weight(o) for o in others), Decimal(0))


async def preview_list(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{items: [{recipe_id, servings, with_optional?}], at_home?: [ingredient ids], others? / extras?} → the
    merged list by aisle, what is at home, the optional articles not bought (« facultatif », to tick), the
    couffin's weight (the typed articles included), the honest cost, the order's ceiling and the 80 DT cap;
    `others` (« + Ajouter autre chose ») come back cleaned, always without a price."""
    data, items = await _items(session, viewer, payload.get("items"))
    at_home = _at_home(payload.get("at_home"), _pantry_defaults(data, [rid for rid, _ in items]))
    opted = _with_optional(payload)
    lst = shopping(data, items, at_home, opted)
    c = recipe_prices.cost(lst["buy"], await recipe_prices.prices(session))
    per_line = c.get("lines", {})
    others = _others(payload.get("others"), payload.get("extras"))
    bought = {ing.id for ing, _ in lst["buy"]} | {i.id for i in lst["home"]}
    optional = optional_left(data, items, opted, bought)
    opt_cost = recipe_prices.cost(
        [(_opt_ing(data, o), Decimal(str(o["qty"]))) for o in optional], await recipe_prices.prices(session)
    ).get("lines", {})
    for o in optional:
        o["price"] = opt_cost.get(o["ingredient_id"])
    return {
        "success": True,
        "recipes": [
            {
                "id": str(rid),
                "title_fr": data.by_id[rid].title_fr,
                "title_ar": data.by_id[rid].title_ar,
                "servings": n,
            }
            for rid, n in items
        ],
        "lines": [_line_doc(ing, q, per_line.get(ing.id)) for ing, q in lst["buy"]],
        "at_home": [{"ingredient_id": i.id, "name_fr": i.name_fr, "name_ar": i.name_ar} for i in lst["home"]],
        "optional": optional,
        "weight_kg": float(lst["weight_kg"] + others_weight(others)),
        "cost": _cost_doc(c),
        **ceiling(c),
        "others": others,
    }


def _opt_ing(data: Loaded, doc: dict[str, Any]) -> ru.Ing:
    """The dictionary ingredient of an optional article (for its price line)."""
    ing = data.ings.get(doc["ingredient_id"]) if isinstance(doc["ingredient_id"], int) else None
    return ing or ru.Ing(
        id=doc["ingredient_id"], key=str(doc["ingredient_id"]),
        name_fr=doc["name_fr"], name_ar=doc["name_ar"],
        unit=doc["unit"], aisle=doc["aisle"], pantry=False, piece_kg=None,
    )  # fmt: skip


# QA 22/10 #5 (decision 6 of the study, « plafond 80 DT au départ »): one recipe order buys at most 80 DT
# of goods (the high end of the estimate); the courier's ceiling = that high end + 15 % (shown to the
# customer and the courier); a ticket above it needs the customer's yes, like the Vitrine.
MAX_RECIPE_BASKET = Decimal("80")


def ceiling(c: dict[str, Any]) -> dict[str, Any]:
    """{cap, over_cap, budget_max}: the 80 DT cap and the order's ceiling (None when no price at all).

    The high end of the KNOWN lines decides (a total refused under 70 % priced still has its known part);
    without a shown total the ceiling is the cap itself (the articles without a price are paid at the
    ticket, within that)."""
    known_high = c.get("known_high") if c.get("available") else None
    over = known_high is not None and Decimal(str(known_high)) > MAX_RECIPE_BASKET
    budget = c.get("budget_max") if c.get("show_total") else None
    if budget is None and c.get("available"):
        budget = int(MAX_RECIPE_BASKET)
    return {"cap": float(MAX_RECIPE_BASKET), "over_cap": over, "budget_max": budget}


# --- the order (placeOrder `order.recipes`) ------------------------------------------------------------


def items_text(
    data: Loaded,
    items: list[tuple[uuid.UUID, int]],
    buy: list[tuple[ru.Ing, Decimal]],
    lang: str,
    test: bool,
    others: list[str] | None = None,
) -> str:
    titles = " · ".join(
        f"{r.title_ar if lang == 'ar' else r.title_fr} ({servings_text(n, r.servings_unit, lang)})"
        for r, n in ((data.by_id[rid], n) for rid, n in items)
    )
    head = f"{TEST_PREFIX if test else ''}{LABEL} {titles}"
    sep = ": " if lang == "ar" else " : "
    lines = [f"{ing.name(lang)}{sep}{ru.fmt_qty(q, ing.unit, lang)}" for ing, q in buy]
    return "\n".join([head, *lines, *(others or [])])


async def nearest_shop(session: AsyncSession, lat: float, lng: float) -> dict[str, Any] | None:
    """The nearest grocery / supermarket of the places index (within 5 km), or None."""
    here = point(lat, lng)
    row = (
        await session.execute(
            select(
                Place.name,
                Place.address,
                Place.city,
                Place.governorate,
                lat_expr(Place.location),
                lng_expr(Place.location),
            )
            .where(
                Place.category == SHOP_CATEGORY, func.ST_DWithin(Place.location, here, NEAREST_SHOP_KM * 1000)
            )
            .order_by(func.ST_Distance(Place.location, here))
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    name, address, city, governorate, plat, plng = row
    return {
        "name": name,
        "address": address,
        "city": city,
        "governorate": governorate,
        "lat": float(plat),
        "lng": float(plng),
    }


_SHOP_WORDS = {
    "sprt": "Supérette", "spr": "Supérette", "superette": "Supérette", "sup": "Supérette",
    "mg": "Magasin Général", "mag": "Magasin", "mgsn": "Magasin", "epic": "Épicerie", "epi": "Épicerie",
}  # fmt: skip


def shop_label(name: str | None) -> str:
    """The places index's raw name, readable (QA 22/10): « Sprt 12 magasin Béchir 12946 » → « Supérette
    magasin Béchir » — known abbreviations spelled out, bare numbers (index ids, branch numbers) dropped."""
    words = []
    for w in str(name or "").split():
        if re.fullmatch(r"[#n°]*\d+", w, flags=re.IGNORECASE):
            continue
        words.append(_SHOP_WORDS.get(w.lower().strip(".")) or w)
    out = " ".join(words).strip(" -·,")
    return (out[:1].upper() + out[1:]) if out else str(name or "")


def _package(weight: Decimal) -> str:
    return "petit" if weight < 3 else "moyen" if weight < 8 else "grand"


async def prepare_order(
    session: AsyncSession, user: User, raw: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """placeOrder with `order.recipes = {items, at_home, accept_unpriced, lang}`: the order rebuilt server
    side (items, estimate, ceiling, shop = the nearest grocery, the courier's note). Raises OrderRefused."""
    from app.security.deps import to_current_user

    r = raw.get("recipes") if isinstance(raw.get("recipes"), dict) else {}
    viewer = to_current_user(user)
    if ADMIN_ONLY and viewer.is_admin and not is_qa_account(user.email):
        # owner 21/10: no real order from the admin's « Ma liste » for now — a test account only (« QA TEST »)
        raise OrderRefused(403, "recipes_order_disabled")
    data, items = await _items(session, viewer, r.get("items"))
    at_home = _at_home(r.get("at_home"), _pantry_defaults(data, [rid for rid, _ in items]))
    opted = _with_optional(r)
    lst = shopping(data, items, at_home, opted)
    others = _others(r.get("others"), r.get("extras"))
    if not lst["buy"] and not others:
        raise OrderRefused(400, "recipes_nothing_to_buy")
    c = recipe_prices.cost(lst["buy"], await recipe_prices.prices(session))
    unpriced = (
        c.get("unpriced", [])
        if c.get("available")
        else [{"id": i.id, "fr": i.name_fr, "ar": i.name_ar} for i, _ in lst["buy"]]
    )
    unpriced = [*unpriced, *({"id": f"o:{o}", "fr": o, "ar": o} for o in others)]
    if unpriced and r.get("accept_unpriced") is not True:
        # the client ticks « j'accepte le prix du ticket pour ces articles » (PRIX-INGREDIENTS §4)
        raise OrderRefused(400, "recipes_unpriced_not_accepted", count=len(unpriced))
    lang = _lang(r.get("lang"))
    test = ADMIN_ONLY or not await public_allowed(session) or is_qa_account(user.email)
    text = items_text(data, items, lst["buy"], lang, test, others)
    if len(text) > ITEMS_TEXT_MAX:
        raise OrderRefused(400, "items_too_long", max=ITEMS_TEXT_MAX, length=len(text))
    lat, lng = raw.get("delivery_lat"), raw.get("delivery_lng")
    shop = None
    try:
        if lat is not None and lng is not None:
            shop = await nearest_shop(session, float(lat), float(lng))
    except (TypeError, ValueError):
        shop = None
    t = TEXTS[lang]
    notes = [t["nearest_note"]]
    if unpriced:
        names = ", ".join(u["ar"] if lang == "ar" else u["fr"] for u in unpriced)
        notes.append(t["unpriced_note"].format(names=names))
    cap = ceiling(c)
    if cap["over_cap"]:
        # QA 22/10 #5: Ma liste says it first (« Plus de 80 DT : retirez une recette… »); the server holds it
        raise OrderRefused(400, "recipes_over_cap", max=cap["cap"], high=float(c["known_high"]))
    budget = cap["budget_max"]
    if budget is not None and Decimal(budget) > MAX_BUDGET:
        raise OrderRefused(400, "invalid_budget", max=float(MAX_BUDGET))
    policy = r.get("unavailable_policy") if r.get("unavailable_policy") in POLICIES else "substitute"
    rebuilt = {
        key: raw.get(key)
        for key in (
            "delivery_address", "delivery_lat", "delivery_lng", "delivery_details", "delivery_governorate",
            "delivery_city", "customer_phone", "preferred_time", "scheduled_time",
        )
    }  # fmt: skip
    rebuilt.update(
        {
            "items_text": text,
            "quantity": 1,
            "notes": " ".join(notes)[:NOTES_MAX],
            "unavailable_policy": policy,
            "estimated_price": c.get("known_high") if c.get("show_total") else None,
            "budget_max": budget,
            "package_size": _package(lst["weight_kg"] + others_weight(others)),
            "shop_name": shop_label(shop["name"]) if shop else t["nearest"],
            "shop_address": (shop or {}).get("address") or "",
            "shop_city": (shop or {}).get("city"),
            "shop_governorate": (shop or {}).get("governorate"),
            "shop_lat": shop["lat"] if shop else lat,
            "shop_lng": shop["lng"] if shop else lng,
        }
    )
    extras = {
        "recipes": [
            {
                "id": str(rid),
                "servings": n,
                "title_fr": data.by_id[rid].title_fr,
                **({"with_optional": sorted(opted[rid], key=str)} if opted.get(rid) else {}),
            }
            for rid, n in items
        ],
        "at_home": sorted(at_home, key=str),
        "lines": [
            *({"ingredient_id": i.id, "qty": float(q), "unit": i.unit} for i, q in lst["buy"]),
            *({"other": o} for o in others),
        ],
        "cost": _cost_doc(c),
        "test": test,
    }
    return rebuilt, extras


async def record_order(
    session: AsyncSession, order_id: uuid.UUID, customer_id: uuid.UUID, extras: dict[str, Any]
) -> None:
    session.add(
        RecipeOrder(
            order_id=order_id,
            customer_id=customer_id,
            recipes=extras["recipes"],
            at_home=extras["at_home"],
            lines=extras["lines"],
            cost=extras["cost"],
            test=extras["test"],
        )
    )


_COUNT_TAIL = re.compile(r"^(.*\S)\s*×\s*(\d{1,2})$")


async def order_again(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """getRecipeOrder {order_id} → what « Ma liste » needs to make a recipe order again (QA 22/10 #5:
    « Commander à nouveau » goes back through the recipes — list rebuilt, ceiling and head line kept —
    never a free order without a ceiling): {recipes: [card + servings + with_optional], at_home, extras}.
    The order's own customer only (or an admin); recipes no longer browsable are left out (`dropped`)."""
    await _require_see(session, viewer)
    oid = _uuid(payload.get("order_id"))
    row = await session.get(RecipeOrder, oid) if oid is not None else None
    if row is None or (viewer is None or (row.customer_id != viewer.id and not viewer.is_admin)):
        raise ApiError(404, "not_found", "Recipe order not found")
    ids = [u for u in (_uuid(r.get("id")) for r in row.recipes or [] if isinstance(r, dict)) if u is not None]
    data = await load(session, ids=ids, published_only=False)
    out, dropped = [], []
    for r in row.recipes or []:
        rid = _uuid(r.get("id")) if isinstance(r, dict) else None
        recipe = data.by_id.get(rid) if rid is not None else None
        if recipe is None or recipe.status not in BROWSABLE:
            dropped.append(str(r.get("title_fr") or "") if isinstance(r, dict) else "")
            continue
        out.append(
            {
                "id": str(recipe.id),
                "slug": recipe.slug,
                "title_fr": recipe.title_fr,
                "title_ar": recipe.title_ar,
                "cuisine": recipe.cuisine,
                "category": recipe.category,
                "servings_unit": recipe.servings_unit,
                "servings": _servings(r.get("servings"), recipe.base_servings),
                "with_optional": list(r.get("with_optional") or []),
                **recipe_photos.fields(recipe),
            }
        )
    extras = []
    for line in row.lines or []:
        if isinstance(line, dict) and isinstance(line.get("other"), str):
            m = _COUNT_TAIL.match(line["other"])
            extras.append(
                {"name": m.group(1), "count": int(m.group(2))} if m else {"name": line["other"], "count": 1}
            )
    return {
        "success": True,
        "recipes": out,
        "at_home": list(row.at_home or []),
        "extras": extras,
        "dropped": dropped,
    }


# --- admin ---------------------------------------------------------------------------------------------


async def admin_list(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    data = await load(session, published_only=False)
    prices = await recipe_prices.prices(session)
    orders = dict(
        (
            await session.execute(
                text(
                    "SELECT e->>'id', count(*) FROM recipe_orders, jsonb_array_elements(recipes) e GROUP BY 1"
                )
            )
        ).all()
    )
    rows = []
    for recipe in data.recipes:
        lst = shopping(data, [(recipe.id, recipe.base_servings)], _pantry_defaults(data, [recipe.id]))
        c = recipe_prices.cost(lst["buy"], prices)
        rows.append(
            {
                **_card(recipe, c, len(data.lines.get(recipe.id, []))),
                "status": recipe.status,
                "published_at": _iso(recipe.published_at),
                "imported_at": _iso(recipe.imported_at),
                "orders": int(orders.get(str(recipe.id), 0)),
            }
        )
    cache_row = await session.get(RecipePriceCache, "TN")
    return {
        "success": True,
        "recipes": rows,
        PUBLIC_ALLOWED: await public_allowed(session),
        "prices": {
            "available": prices is not None,
            "fetched_at": _iso(cache_row.fetched_at) if cache_row else None,
            "priced": len(recipe_prices.price_index(prices)),
        },
    }


MAX_BULK = 1000


async def set_status(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """{id | ids: [...], status} → one recipe, or many at once (« Publier les recettes affichées »)."""
    _admin(admin)
    wanted = payload.get("status")
    if wanted not in ("draft", "published"):
        raise _invalid("status: draft or published")
    raw_ids = payload.get("ids") if isinstance(payload.get("ids"), list) else [payload.get("id")]
    ids = [i for i in (_uuid(v) for v in raw_ids[:MAX_BULK]) if i is not None]
    recipes = list((await session.execute(select(Recipe).where(Recipe.id.in_(ids)))).scalars()) if ids else []
    if not recipes:
        raise ApiError(404, "not_found", "Recipe not found")
    with_lines = set(
        (
            await session.execute(
                select(RecipeLine.recipe_id).where(RecipeLine.recipe_id.in_(ids)).distinct()
            )
        ).scalars()
    )
    if wanted == "published" and len(recipes) == 1 and recipes[0].id not in with_lines:
        raise ApiError(409, "recipe_incomplete", "A recipe without ingredients cannot be published")
    now = datetime.now(UTC)
    changed = []
    for recipe in recipes:
        if recipe.status == wanted or (wanted == "published" and recipe.id not in with_lines):
            continue
        before = recipe.status
        recipe.status = wanted
        recipe.published_at = now if wanted == "published" else None
        recipe.published_by = admin.id if wanted == "published" else None
        changed.append(recipe)
        session.add(
            AuditLog(
                actor_user_id=admin.id,
                action="recipes.status",
                entity="recipes",
                entity_id=str(recipe.id),
                before={"status": before},
                after={"status": wanted},
            )
        )
    await session.flush()
    first = recipes[0]
    return {
        "success": True,
        "id": str(first.id),
        "status": first.status,
        "published_at": _iso(first.published_at),
        "changed": len(changed),
    }
