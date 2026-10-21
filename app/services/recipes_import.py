"""Import of the recipes (recettes.json) and of the ingredient dictionary — idempotent.

- Ingredients: from the referential CSV (ods-audit/recettes-delivery-2110/mesure/referentiel_ingredients.csv:
  id, key, fr, ar, unit, piece_kg, density, aisle, pantry) or from the ODS price answer (`ingredients[]`:
  id, key, name_fr, name_ar, buy_unit, piece_kg, aisle, pantry). Upsert by id; never a price.
- Recipes: upsert by slug; the lines are replaced. A new recipe is a draft; a published recipe whose content
  changed goes back to draft (an admin publishes it again after a look); an unchanged one keeps its status.
- Several files at once (recettes.json + recettes_<groupe>.json, same schema + `cuisine` / `pays`). A line
  without a dictionary reference (`ingredient_new`, an unknown id) is KEPT with its own names, unit and aisle
  (from ingredients_a_ajouter_<groupe>.json): it stays without a price, never blocking (a warning says so).
  An unreadable line is skipped with a warning; a recipe without title or readable ingredient is refused.

Used by the admin function importAdminRecipes and the command `python -m scripts.import_recipes`.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ingredient, Recipe, RecipeLine
from app.models.recipes import AISLES, INGREDIENT_UNITS
from app.services import recipe_units as ru
from app.services import text_norm
from app.services.recipes import fold, to_ing

MAX_RECIPES = 2000
MAX_LINES = 40
MAX_STEPS = 40
ONE = Decimal(1)
UNIT_ALIASES = {
    "kg": ("kg", ONE), "g": ("kg", Decimal("0.001")), "l": ("l", ONE), "cl": ("l", Decimal("0.01")),
    "ml": ("l", Decimal("0.001")), "piece": ("piece", ONE), "pièce": ("piece", ONE), "pieces": ("piece", ONE),
    "pièces": ("piece", ONE), "pc": ("piece", ONE), "botte": ("botte", ONE), "bottes": ("botte", ONE),
    "cas": ("cas", ONE), "c. à s.": ("cas", ONE), "c.à.s": ("cas", ONE), "cs": ("cas", ONE),
    "cac": ("cac", ONE), "c. à c.": ("cac", ONE), "c.à.c": ("cac", ONE), "cc": ("cac", ONE),
    "c. à soupe": ("cas", ONE), "c. à café": ("cac", ONE), "pincée": ("pincee", ONE),
    "pincee": ("pincee", ONE), "pincées": ("pincee", ONE),
}  # fmt: skip


def _first(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if "." in key:
            outer, inner = key.split(".", 1)
            v = d.get(outer)
            v = v.get(inner) if isinstance(v, dict) else None
        else:
            v = d.get(key)
        if v not in (None, "", []):
            return v
    return None


def _text(value: Any, limit: int = 300) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def _dec(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "oui", "yes")


def slugify(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:80]


# --- ingredients -----------------------------------------------------------------------------------------


def ingredient_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One dictionary entry (CSV or ODS shape) → the table's columns; None when unusable."""
    try:
        iid = int(str(raw.get("id")).strip())
    except (TypeError, ValueError):
        return None
    unit = str(_first(raw, "buy_unit", "unit") or "").strip().lower()
    aisle = str(raw.get("aisle") or "").strip().lower()
    key = _text(raw.get("key"), 120)
    fr = _text(_first(raw, "name_fr", "fr"), 120)
    ar = _text(_first(raw, "name_ar", "ar"), 120)
    if unit not in INGREDIENT_UNITS or aisle not in AISLES or not (key and fr):
        return None
    piece_kg = _dec(raw.get("piece_kg"))
    density = _dec(raw.get("density"))
    return {
        "id": iid,
        "key": key,
        "name_fr": fr,
        "name_ar": ar or fr,
        "unit": unit,
        "aisle": aisle,
        "pantry": _bool(raw.get("pantry")),
        "piece_kg": piece_kg if piece_kg and piece_kg > 0 else None,
        "density": density if density and density > 0 else None,
    }


async def import_ingredients(session: AsyncSession, entries: list[dict[str, Any]]) -> dict[str, int]:
    created = updated = refused = 0
    existing = {i.id: i for i in (await session.execute(select(Ingredient))).scalars()}
    for raw in entries:
        row = ingredient_row(raw) if isinstance(raw, dict) else None
        if row is None:
            refused += 1
            continue
        found = existing.get(row["id"])
        if found is None:
            session.add(Ingredient(**row))
            created += 1
        elif any(getattr(found, k) != v for k, v in row.items()):
            for k, v in row.items():
                setattr(found, k, v)
            updated += 1
    await session.flush()
    return {"created": created, "updated": updated, "refused": refused}


# --- recipes ---------------------------------------------------------------------------------------------


def _steps(raw: dict[str, Any], lang: str) -> list[str]:
    value = _first(raw, f"etapes_{lang}", f"steps_{lang}", f"steps.{lang}", f"etapes.{lang}")
    if value is None and isinstance(raw.get("steps"), list):
        value = [s.get(lang) for s in raw["steps"] if isinstance(s, dict)]
    if isinstance(value, str):
        value = [value]
    return [t for t in (_text(s, 1000) for s in (value or [])[:MAX_STEPS]) if t]


def new_ingredients_of(document: Any) -> dict[str, dict[str, Any]]:
    """{key: entry} of the « ingrédients à ajouter » (a file's `ingredients_a_ajouter`, or a list / an
    `ingredients` object of ingredients_a_ajouter_<groupe>.json): aisle, unit, weight of a piece / bunch."""
    entries: Any = document
    if isinstance(document, dict):
        entries = document.get("ingredients_a_ajouter") or document.get("ingredients") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            key = str(_first(entry, "key", "cle", "fr") or "").strip().lower()
            if key:
                out[key] = entry
    return out


SERVINGS_UNITS = {
    "personnes": "people", "personne": "people", "people": "people", "pers": "people",
    "pièces": "pieces", "pièce": "pieces", "pieces": "pieces", "piece": "pieces",
    "portions": "portions", "portion": "portions",
}  # fmt: skip


def _servings_unit(raw: dict[str, Any]) -> str:
    """`portions_unite` of the files: a pastry is written for « 30 pièces », a spice mix for « 10 portions »
    (people when absent or unknown)."""
    value = str(_first(raw, "portions_unite", "servings_unit") or "").strip().lower().rstrip(".")
    return SERVINGS_UNITS.get(value, "people")


def _region_cuisine(raw: dict[str, Any]) -> str | None:
    """The first file (recettes.json, 40 Tunisian recipes) has neither `cuisine` nor `pays` (the other files
    always do): such a recipe is Tunisian."""
    return "tunisienne" if not raw.get("cuisine") and not raw.get("pays") and not raw.get("country") else None


def _local_title(raw: dict[str, Any]) -> str | None:
    value = _first(raw, "title_local", "nom_local", "titre_local")
    if isinstance(value, dict):
        value = value.get("latin") or value.get("fr") or value.get("ar")
    return _text(value, 120)


def parse_line(
    pos: int,
    line: Any,
    slug: str,
    ings: dict[int, ru.Ing],
    by_key: dict[str, int],
    new: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """One ingredient line → (columns, warning). A line without a dictionary reference is KEPT (never
    blocking): its names, unit and aisle are stored and it stays without a price."""
    if not isinstance(line, dict):
        return None, f"{slug}: ligne {pos + 1} illisible (ignorée)"
    ref = line.get("ingredient_id")
    ing: ru.Ing | None = None
    if ref not in (None, ""):
        try:
            ing = ings.get(int(str(ref).strip()))
        except ValueError:
            ing = ings.get(by_key.get(str(ref).strip().lower(), -1))
    new_key = str(_first(line, "ingredient_new", "ingredient_a_ajouter") or "").strip().lower() or None
    if ing is None and new_key is not None and new_key in by_key:
        # the dictionary grew (ODS added it with an id): the line gets its real reference, and maybe a price
        ing, new_key = ings[by_key[new_key]], None
    if ing is None and new_key is None and isinstance(line.get("key"), str):
        ing = ings.get(by_key.get(line["key"].strip().lower(), -1))
    name_fr = _text(_first(line, "nom_fr", "name_fr"), 120) or (ing.name_fr if ing else None) or new_key
    name_ar = _text(_first(line, "nom_ar", "name_ar"), 120) or (ing.name_ar if ing else None) or name_fr
    if not name_fr:
        return None, f"{slug}: ligne {pos + 1} sans nom (ignorée)"
    warning = None
    if ing is None and new_key is None:
        new_key = slugify(name_fr)
        warning = f"{slug}: « {name_fr} » ({ref}) hors dictionnaire : gardé sans prix"
    qty = _dec(_first(line, "quantite", "qty", "quantity"))
    alias = UNIT_ALIASES.get(str(_first(line, "unite", "unit") or (ing.unit if ing else "")).strip().lower())
    if qty is None or qty <= 0 or alias is None:
        return None, f"{slug}: quantité ou unité illisible pour {name_fr} (ligne ignorée)"
    unit, factor = alias
    qty = qty * factor
    if not 0 < qty <= 100:
        return None, f"{slug}: quantité hors limites pour {name_fr} (ligne ignorée)"
    if ing is not None and ru.to_buy_unit(qty, unit, ing) is None:
        warning = f"{slug}: « {name_fr} » en {unit} alors qu'il s'achète en {ing.unit} : gardé sans prix"
    meta = new.get(new_key or "", {}) if ing is None else {}
    aisle = str(meta.get("rayon") or meta.get("aisle") or "").strip().lower()
    piece_kg = _dec(meta.get("piece_kg"))
    return {
        "position": pos,
        "ingredient_id": ing.id if ing else None,
        "new_key": None if ing else new_key,
        "name_fr": name_fr,
        "name_ar": name_ar,
        "aisle": None if ing else (aisle if aisle in AISLES else "epicerie"),
        "piece_kg": piece_kg if ing is None and piece_kg and piece_kg > 0 else None,
        "pantry": _bool(_first(line, "placard", "pantry"))
        if any(k in line for k in ("placard", "pantry"))
        else bool(ing and ing.pantry),
        "qty": qty.quantize(Decimal("0.0001")),
        "unit": unit,
        "optional": _bool(_first(line, "optionnel", "optional", "facultatif")),
        "note_fr": _text(_first(line, "note_fr", "note"), 200),
        "note_ar": _text(line.get("note_ar"), 200),
    }, warning


def parse_recipe(
    raw: dict[str, Any], ings: dict[int, ru.Ing], by_key: dict[str, int], new: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """→ (columns + lines, None, warnings) or (None, reason, warnings)."""
    title_fr = _text(_first(raw, "nom_fr", "title_fr", "titre_fr", "title.fr", "titre.fr"), 120)
    title_ar = _text(_first(raw, "nom_ar", "title_ar", "titre_ar", "title.ar", "titre.ar"), 120)
    slug = slugify(str(_first(raw, "id", "slug") or title_fr or ""))
    if not slug or not title_fr:
        return None, "titre ou identifiant manquant", []
    servings = _dec(_first(raw, "portions", "base_servings", "servings", "personnes")) or Decimal(4)
    if not 1 <= servings <= 100:  # a pastry is often written for 30 pieces
        return None, f"{slug}: nombre de portions hors 1–100", []
    time_min = _dec(_first(raw, "time_min", "temps_min", "total_time_min"))
    prep_min = _dec(_first(raw, "prep_min", "preparation_min"))
    cook_min = _dec(_first(raw, "cuisson_min", "cook_min", "cooking_min"))
    if time_min is None:
        time_min = sum((p for p in (prep_min, cook_min) if p), Decimal(0)) or None
    lines_raw = _first(raw, "ingredients", "lignes", "lines") or []
    if not isinstance(lines_raw, list) or not lines_raw:
        return None, f"{slug}: aucun ingrédient", []
    lines: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line in lines_raw[:MAX_LINES]:
        parsed, warning = parse_line(len(lines), line, slug, ings, by_key, new)
        if warning:
            warnings.append(warning)
        if parsed is not None:
            lines.append(parsed)
    if not lines:
        return None, f"{slug}: aucun ingrédient lisible", warnings
    tags = _first(raw, "tags", "occasion")
    tags = tags if isinstance(tags, list) else []
    sources = raw.get("sources") if isinstance(raw.get("sources"), list) else []
    local = _local_title(raw)
    cols = {
        "slug": slug,
        "title_fr": title_fr,
        "title_ar": title_ar or title_fr,
        "title_local": local,
        "cuisine": (_text(raw.get("cuisine"), 40) or "").lower() or _region_cuisine(raw),
        "country": _text(_first(raw, "pays", "country"), 60) or ("Tunisie" if _region_cuisine(raw) else None),
        "category": (_text(_first(raw, "categorie", "category"), 40) or "plat").lower(),
        "time_min": int(time_min) if time_min and 1 <= time_min <= 1440 else None,
        "difficulty": _text(_first(raw, "difficulte", "difficulty"), 20),
        "base_servings": int(servings),
        "servings_unit": _servings_unit(raw),
        "tags": [t for t in (_text(x, 30) for x in tags[:20]) if t],
        "intro_fr": _text(_first(raw, "intro_fr", "description_fr"), 500),
        "intro_ar": _text(_first(raw, "intro_ar", "description_ar"), 500),
        "tip_fr": _text(_first(raw, "astuce_fr", "tip_fr"), 600),
        "tip_ar": _text(_first(raw, "astuce_ar", "tip_ar"), 600),
        "sources": [u for u in (_text(x, 300) for x in sources[:10]) if u],
        "steps_fr": _steps(raw, "fr"),
        "steps_ar": _steps(raw, "ar"),
    }
    cols["search_norm"] = fold(
        title_fr,
        title_ar,
        local or "",
        (raw.get("nom_local") or {}).get("ar", "") if isinstance(raw.get("nom_local"), dict) else "",
    )
    digest = json.dumps({**cols, "lines": lines}, sort_keys=True, default=str, ensure_ascii=False)
    cols["content_hash"] = hashlib.sha256(digest.encode()).hexdigest()
    # the two parts of the time (22/10, the fiche's time ring) stay OUT of the content hash: filling them on
    # the recipes already imported must not send a published recipe back to draft
    cols["prep_min"] = _minutes(prep_min)
    cols["cook_min"] = _minutes(cook_min)
    cols["seasons"] = seasons_of(_first(raw, "saison", "season", "seasons"))
    return {**cols, "lines": lines}, None, warnings


ALL_SEASONS = ["hiver", "printemps", "ete", "automne"]


def seasons_of(value: Any) -> list[str]:
    """« Automne et hiver », « Toute l'année, Ramadan », « Automne à printemps » → season words."""
    raw = " ".join(value) if isinstance(value, list) else str(value or "")
    t = text_norm.normalize_text(raw)
    out: list[str] = []
    if "toute l" in t or "toute l annee" in t or "all year" in t:
        out += ALL_SEASONS
    if "automne a printemps" in t:
        out += ["automne", "hiver", "printemps"]
    for word, key in (("hiver", "hiver"), ("printemps", "printemps"), ("ete", "ete"), ("automne", "automne"),
                      ("ramadan", "ramadan"), ("aid", "aid")):  # fmt: skip
        if re.search(rf"\b{word}\b", t):
            out.append(key)
    return [k for k in [*ALL_SEASONS, "ramadan", "aid"] if k in out]


def _minutes(value: Decimal | None) -> int | None:
    return int(value) if value is not None and 0 <= value <= 1440 else None


def recipes_of(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("recettes", "recipes"):
            if isinstance(document.get(key), list):
                return document[key]
    return []


async def import_recipes(
    session: AsyncSession, documents: Any, new_ingredients: Any = None
) -> dict[str, Any]:
    """One or several recipe files (each a list or an object with `recettes`), idempotent."""
    docs = (
        documents
        if isinstance(documents, list)
        and documents
        and all(isinstance(d, dict) and recipes_of(d) for d in documents)
        else [documents]
    )
    new: dict[str, dict[str, Any]] = {}
    for doc in docs:
        new.update(new_ingredients_of(doc))
    for extra in new_ingredients if isinstance(new_ingredients, list) else []:
        new.update(new_ingredients_of(extra))
    entries = [r for doc in docs for r in recipes_of(doc)][:MAX_RECIPES]
    ings = {i.id: to_ing(i) for i in (await session.execute(select(Ingredient))).scalars()}
    by_key = {i.key: i.id for i in ings.values()}
    existing = {r.slug: r for r in (await session.execute(select(Recipe))).scalars()}
    report: dict[str, Any] = {
        "created": 0, "updated": 0, "unchanged": 0, "times": 0, "back_to_draft": [], "refused": [],
        "warnings": [],
    }  # fmt: skip
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            report["refused"].append("entrée illisible")
            continue
        parsed, reason, warnings = parse_recipe(raw, ings, by_key, new)
        report["warnings"].extend(warnings)
        if parsed is None:
            report["refused"].append(reason)
            continue
        if parsed["slug"] in seen:
            report["refused"].append(f"{parsed['slug']}: en double")
            continue
        seen.add(parsed["slug"])
        lines = parsed.pop("lines")
        recipe = existing.get(parsed["slug"])
        if recipe is not None and recipe.content_hash == parsed["content_hash"]:
            report["unchanged"] += 1
            extra = ("prep_min", "cook_min", "seasons")
            if any(getattr(recipe, k) != parsed[k] for k in extra):
                for k in extra:
                    setattr(recipe, k, parsed[k])
                report["times"] += 1
            continue
        if recipe is None:
            recipe = Recipe(**parsed, status="draft")
            session.add(recipe)
            await session.flush()
            report["created"] += 1
        else:
            for k, v in parsed.items():
                setattr(recipe, k, v)
            if recipe.status == "published":
                recipe.status, recipe.published_at, recipe.published_by = "draft", None, None
                report["back_to_draft"].append(recipe.slug)
            report["updated"] += 1
            await session.execute(delete(RecipeLine).where(RecipeLine.recipe_id == recipe.id))
        recipe.imported_at = func.now()
        session.add_all(RecipeLine(recipe_id=recipe.id, **line) for line in lines)
        await session.flush()
    report["warnings"] = report["warnings"][:200]
    return report
