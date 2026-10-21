"""Import the recipes (and the ingredient dictionary) — idempotent, safe to run again.

    uv run python -m scripts.import_recipes --dir <dossier> [--ingredients referentiel.csv | --from-ods]
    (--ingredients also takes ODS's dictionary file itself, tn_ingredients.json)
    uv run python -m scripts.import_recipes --recipes recettes.json recettes_maghreb.json …

`--dir` reads every recettes*.json and ingredients_a_ajouter*.json of the folder. Same rules as the admin
function importAdminRecipes (app/services/recipes_import.py): upsert by slug, a new recipe is a draft, a
published recipe whose content changed goes back to draft. Prints the report; nothing is published here.
"""

import argparse
import asyncio
import csv
import json
from pathlib import Path

from app.db import transaction
from app.services import recipes_import


def _read(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _files(args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    recipes = list(args.recipes or [])
    extra = list(args.new_ingredients or [])
    if args.dir is not None:
        recipes += sorted(args.dir.glob("recettes*.json"))
        extra += sorted(args.dir.glob("ingredients_a_ajouter*.json"))
    return recipes, extra


async def run(args: argparse.Namespace) -> dict:
    report: dict = {}
    recipe_files, extra_files = _files(args)
    async with transaction() as session:
        if args.from_ods:
            # the dictionary ODS sends with its prices (it grows with the recipes: ids 151, 152…)
            from app.services import recipe_prices

            payload = await recipe_prices.prices(session)
            if not payload:
                raise SystemExit("ODS prices not reachable (ODS_API_BASE_URL / ODS_RECIPES_SECRET)")
            report["ingredients_ods"] = await recipes_import.import_ingredients(
                session, payload["ingredients"]
            )
        if args.ingredients is not None:
            with args.ingredients.open(encoding="utf-8") as handle:
                if args.ingredients.suffix == ".csv":
                    rows = list(csv.DictReader(handle))
                else:
                    # ODS's dictionary file itself (tn_ingredients.json: a list) or {"ingredients": [...]}
                    data = json.load(handle)
                    rows = data if isinstance(data, list) else data.get("ingredients", [])
            report["ingredients"] = await recipes_import.import_ingredients(session, rows)
        if recipe_files:
            docs = [await asyncio.to_thread(_read, p) for p in recipe_files]
            extra = [await asyncio.to_thread(_read, p) for p in extra_files]
            report["files"] = [p.name for p in recipe_files]
            report.update(await recipes_import.import_recipes(session, docs, extra))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recipes", type=Path, nargs="*")
    parser.add_argument("--new-ingredients", type=Path, nargs="*")
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--ingredients", type=Path)
    parser.add_argument("--from-ods", action="store_true", help="dictionary from the ODS price API")
    args = parser.parse_args()
    if not (args.recipes or args.dir or args.ingredients or args.from_ods):
        parser.error("--dir, --recipes and/or --ingredients")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
