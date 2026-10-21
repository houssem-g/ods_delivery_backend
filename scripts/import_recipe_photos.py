"""Record the dish photos of the recipes from a JSON file — idempotent, safe to run again.

    uv run python -m scripts.import_recipe_photos photos.json [--source ia|photo]

The file: {"<slug>": "<url or key>" | {"photo": …, "thumb": …, "alt_fr": …, "alt_ar": …} | null, …}
(or a list of {"slug": …, "photo": …}). Same rules as the admin function setRecipePhoto
(app/services/recipe_photos.py): a key under public/recipes/ ending in .webp, or a URL of our own public
files origin; null removes the photo. The files themselves are uploaded to the bucket beforehand
(public/recipes/<slug>.webp and, optional, public/recipes/<slug>-480.webp). Prints the report.
"""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.db import transaction
from app.services import recipe_photos

# the import is an operator's act: the audit rows carry no user (actor NULL), like scripts.import_recipes
OPERATOR = SimpleNamespace(id=None, is_admin=True)


async def run(path: Path, source: str | None) -> dict:
    data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    if source and isinstance(data, dict):
        data = {k: ({"photo": v, "source": source} if isinstance(v, str) else v) for k, v in data.items()}
    async with transaction() as session:
        return await recipe_photos.set_photos(session, OPERATOR, {"photos": data})  # type: ignore[arg-type]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--source", choices=("ia", "photo"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.file, args.source)), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
