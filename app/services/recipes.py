"""Recettes (21/10/2026, first version): « je choisis une recette, je dis pour combien de personnes, je coche
ce que j'ai déjà, et un livreur m'apporte le reste ».

Spec: ods-audit/recettes-delivery-2110/ETUDE.md (T2, T4) and PRIX-INGREDIENTS.md (honest cost). The order a
recipe list makes is a NORMAL Achats order (placeOrder → offers, « Article indisponible », Offre Chaude…,
nothing changed): `items_text` = one line per article the courier ticks, rebuilt here server side from the
recipes, the people and the « j'ai déjà » — never the client's text.

Visibility (like the Vitrine at its start), enforced HERE: admins and the test accounts (settings.QA_ACCOUNTS)
see the published recipes; everybody else only once the switch « Recettes publiques »
(`app_settings['recipes'].public_allowed`, off) is on — before that the client reads `visible: false` and an
unknown recipe. A test account's (or any, while not public) recipe order carries « QA TEST »: offered to
the QA couriers only (existing dispatch rule).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
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
from app.services import recipe_prices, text_norm
from app.services import recipe_units as ru
from app.services.orders import MAX_BUDGET, OrderRefused, is_qa_account
from app.services.places import lat_expr, lng_expr, point

SETTINGS_KEY = "recipes"
PUBLIC_ALLOWED = "public_allowed"
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
        "nearest": "Supermarché ou épicerie le plus proche",
        "nearest_note": "Courses de recettes : magasin le plus proche, au choix du livreur.",
        "unpriced_note": "Sans prix connu (le client accepte le prix du ticket) : {names}.",
    },
    "ar": {
        "people": "أشخاص",
        "nearest": "أقرب مغازة أو عطّار",
        "nearest_note": "مشتريات وصفات: أقرب محل، يختاره المندوب.",
        "unpriced_note": "بدون سعر معروف (الحريف يقبل سعر الوصل): {names}.",
    },
}


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
    if viewer.is_admin or is_qa_account(viewer.email):
        return True
    return await public_allowed(session)


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


def needs(data: Loaded, items: list[tuple[uuid.UUID, int]]) -> list[tuple[ru.Ing, Decimal]]:
    """What the recipes × people need, in buying units, summed by ingredient (not rounded yet)."""
    out: list[tuple[ru.Ing, Decimal]] = []
    for recipe_id, servings in items:
        recipe = data.by_id[recipe_id]
        for line in data.lines.get(recipe_id, []):
            ing, q = line_need(data, line, ru.scale(line.qty, servings, recipe.base_servings))
            if q > 0:
                out.append((ing, q))
    return ru.merge(out)


def shopping(data: Loaded, items: list[tuple[uuid.UUID, int]], at_home: set[int]) -> dict[str, Any]:
    """The list to buy (rounded, by aisle), what is at home, the weight."""
    merged = needs(data, items)
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


def _card(recipe: Recipe, c: dict[str, Any], lines_n: int) -> dict[str, Any]:
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
        "difficulty": recipe.difficulty,
        "base_servings": recipe.base_servings,
        "tags": list(recipe.tags or []),
        "ingredients_count": lines_n,
        "cost": _cost_doc(c),
        "per_person": recipe_prices.per_person(c, recipe.base_servings),
    }


PAGE_SIZE = 20
PAGE_SIZE_MAX = 40
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})


def fold(*values: Any) -> str:
    """Titles / a query folded the same way: no case, no accent, Arabic letters unified."""
    return text_norm.normalize_text(*values).translate(_AR_FOLD)


def _filters(payload: dict[str, Any]) -> list[Any]:
    where: list[Any] = [Recipe.status == "published"]
    for field, column in (
        ("cuisine", Recipe.cuisine),
        ("country", Recipe.country),
        ("category", Recipe.category),
    ):
        value = str(payload.get(field) or "").strip()
        if value and value != "all":
            where.append(column == value[:60])
    try:
        max_time = int(payload.get("max_time") or 0)
    except (TypeError, ValueError):
        max_time = 0
    if max_time > 0:
        where.append(Recipe.time_min <= max_time)
    for token in fold(str(payload.get("q") or "")[:80]).split()[:6]:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(Recipe.search_norm.like(f"%{escaped}%"))
    return where


async def _facets(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """The values of cuisine / country / category among the published recipes, with their counts."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name, column in (
        ("cuisines", Recipe.cuisine),
        ("countries", Recipe.country),
        ("categories", Recipe.category),
    ):
        rows = (
            await session.execute(
                select(column, func.count())
                .where(Recipe.status == "published", column.is_not(None))
                .group_by(column)
                .order_by(func.count().desc(), column)
            )
        ).all()
        out[name] = [{"key": key, "count": n} for key, n in rows]
    return out


async def list_recipes(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{q?, cuisine?, country?, category?, max_time?, page? (1…), page_size? (≤ 40)} → one page of cards, the
    total, the facets (on page 1 only). Costs are computed for the page only."""
    if not await can_see(session, viewer):
        return {"success": True, "visible": False, "recipes": [], "total": 0}
    try:
        page = max(1, int(payload.get("page") or 1))
        size = max(1, min(PAGE_SIZE_MAX, int(payload.get("page_size") or PAGE_SIZE)))
    except (TypeError, ValueError):
        page, size = 1, PAGE_SIZE
    where = _filters(payload)
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
    data = await load(session, ids=ids)
    prices = await recipe_prices.prices(session) if ids else None
    cards = []
    for recipe in data.recipes:
        lst = shopping(data, [(recipe.id, recipe.base_servings)], _pantry_defaults(data, [recipe.id]))
        cards.append(
            _card(recipe, recipe_prices.cost(lst["buy"], prices), len(data.lines.get(recipe.id, [])))
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
        answer["facets"] = await _facets(session)
    return answer


async def _visible_recipe(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> Recipe:
    if not await can_see(session, viewer):
        raise ApiError(404, "not_found", "Recipe not found")
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
    """{id | slug, servings?, at_home?} → the recipe: exact quantities to cook for `servings`, the list to buy
    (rounded) without what is at home (pantry pre-ticked unless `at_home` is sent), its honest cost."""
    recipe = await _visible_recipe(session, viewer, payload)
    data = await load(session, ids=[recipe.id], published_only=False)
    servings = _servings(payload.get("servings"), recipe.base_servings)
    pantry = _pantry_defaults(data, [recipe.id])
    at_home = _at_home(payload.get("at_home"), pantry)
    lst = shopping(data, [(recipe.id, servings)], at_home)
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
            **_card(recipe, c, len(ingredients)),
            "status": recipe.status,
            "intro_fr": recipe.intro_fr,
            "intro_ar": recipe.intro_ar,
            "tip_fr": recipe.tip_fr,
            "tip_ar": recipe.tip_ar,
            "steps_fr": list(recipe.steps_fr or []),
            "steps_ar": list(recipe.steps_ar or []),
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
        raise OrderRefused(404, "recipes_unavailable")
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
    data = await load(session, ids=list(wanted))
    missing = [str(rid) for rid in wanted if rid not in data.by_id]
    if missing:
        raise OrderRefused(409, "recipes_unavailable", ids=missing)
    return data, list(wanted.items())


async def preview_list(
    session: AsyncSession, viewer: CurrentUser | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """{items: [{recipe_id, servings}], at_home?: [ingredient ids]} → the merged list by aisle, what is at
    home, the couffin's weight, the honest cost (and the order's ceiling when a total is shown)."""
    data, items = await _items(session, viewer, payload.get("items"))
    at_home = _at_home(payload.get("at_home"), _pantry_defaults(data, [rid for rid, _ in items]))
    lst = shopping(data, items, at_home)
    c = recipe_prices.cost(lst["buy"], await recipe_prices.prices(session))
    per_line = c.get("lines", {})
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
        "weight_kg": float(lst["weight_kg"]),
        "cost": _cost_doc(c),
    }


# --- the order (placeOrder `order.recipes`) ------------------------------------------------------------


def items_text(
    data: Loaded, items: list[tuple[uuid.UUID, int]], buy: list[tuple[ru.Ing, Decimal]], lang: str, test: bool
) -> str:
    t = TEXTS[lang]
    titles = " · ".join(
        f"{data.by_id[rid].title_ar if lang == 'ar' else data.by_id[rid].title_fr} ({n} {t['people']})"
        for rid, n in items
    )
    head = f"{TEST_PREFIX if test else ''}{LABEL} {titles}"
    sep = ": " if lang == "ar" else " : "
    lines = [f"{ing.name(lang)}{sep}{ru.fmt_qty(q, ing.unit, lang)}" for ing, q in buy]
    return "\n".join([head, *lines])


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
    data, items = await _items(session, viewer, r.get("items"))
    at_home = _at_home(r.get("at_home"), _pantry_defaults(data, [rid for rid, _ in items]))
    lst = shopping(data, items, at_home)
    if not lst["buy"]:
        raise OrderRefused(400, "recipes_nothing_to_buy")
    c = recipe_prices.cost(lst["buy"], await recipe_prices.prices(session))
    unpriced = (
        c.get("unpriced", [])
        if c.get("available")
        else [{"id": i.id, "fr": i.name_fr, "ar": i.name_ar} for i, _ in lst["buy"]]
    )
    if unpriced and r.get("accept_unpriced") is not True:
        # the client ticks « j'accepte le prix du ticket pour ces articles » (PRIX-INGREDIENTS §4)
        raise OrderRefused(400, "recipes_unpriced_not_accepted", count=len(unpriced))
    lang = _lang(r.get("lang"))
    test = not await public_allowed(session) or is_qa_account(user.email)
    text = items_text(data, items, lst["buy"], lang, test)
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
    budget = c.get("budget_max") if c.get("show_total") else None
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
            "package_size": _package(lst["weight_kg"]),
            "shop_name": shop["name"] if shop else t["nearest"],
            "shop_address": (shop or {}).get("address") or "",
            "shop_city": (shop or {}).get("city"),
            "shop_governorate": (shop or {}).get("governorate"),
            "shop_lat": shop["lat"] if shop else lat,
            "shop_lng": shop["lng"] if shop else lng,
        }
    )
    extras = {
        "recipes": [
            {"id": str(rid), "servings": n, "title_fr": data.by_id[rid].title_fr} for rid, n in items
        ],
        "at_home": sorted(at_home),
        "lines": [{"ingredient_id": i.id, "qty": float(q), "unit": i.unit} for i, q in lst["buy"]],
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
