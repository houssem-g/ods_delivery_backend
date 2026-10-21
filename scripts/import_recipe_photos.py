"""Record the dish photos of the recipes from a JSON file — idempotent, safe to run again.

    uv run python -m scripts.import_recipe_photos photos.json [--source ia|photo] [--upload] [--dry-run]

The file, either the photo team's (photos/libres/photos_<x>.json):
    {"<slug>": {"file": "/…/<slug>.webp", "source_page": …, "image_url": …, "author": …,
                "license": "CC BY-SA 4.0", "license_url": …, "title": …, "note": …}, …}
or the short form {"<slug>": "<url or key>" | {"photo": …, "thumb": …, "alt_fr": …, "alt_ar": …} | null, …}
(or a list of {"slug": …, "photo": …}). Same rules as the admin function setRecipePhoto
(app/services/recipe_photos.py): a key under public/recipes/ ending in .webp, or a URL of our own public files
origin; null removes the photo; an entry with only "file" gets the key public/recipes/<slug>.webp and its
credit.

--upload: first puts each entry's local "file" (WebP) at public/recipes/<slug>.webp in the bucket (public ACL,
cached a year), then records the photos; an entry whose file is missing is reported and skipped.
--dry-run: reads and checks the file, prints what would be recorded, writes nothing.
"""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.db import transaction
from app.services import recipe_photos
from app.storage import s3

# the import is an operator's act: the audit rows carry no user (actor NULL), like scripts.import_recipes
OPERATOR = SimpleNamespace(id=None, is_admin=True)
CACHE = "public, max-age=31536000, immutable"


def _normalise(data: Any, source: str | None) -> dict[str, Any]:
    if isinstance(data, list):
        data = {str(p.get("slug")): p for p in data if isinstance(p, dict) and p.get("slug")}
    if not isinstance(data, dict):
        raise SystemExit("photos file: an object {slug: …} or a list [{slug, …}]")
    if source:
        data = {k: _with_source(v, source) for k, v in data.items()}
    return data


def _with_source(value: Any, source: str) -> Any:
    if isinstance(value, str):
        return {"photo": value, "source": source}
    return {**value, "source": source} if isinstance(value, dict) else value


async def upload(data: dict[str, Any]) -> dict[str, Any]:
    """Puts the local WebP files in the bucket; drops the entries whose file is missing."""
    report: dict[str, Any] = {"uploaded": 0, "missing_file": []}
    for slug, entry in list(data.items()):
        if not isinstance(entry, dict) or not entry.get("file"):
            continue
        path = Path(str(entry["file"]))
        if path.suffix.lower() != ".webp" or not await asyncio.to_thread(path.is_file):
            report["missing_file"].append(slug)
            data.pop(slug)
            continue
        body = await asyncio.to_thread(path.read_bytes)
        await s3.put_object(recipe_photos.default_key(slug), body, "image/webp", cache_control=CACHE)
        report["uploaded"] += 1
    return report


async def run(path: Path, source: str | None, *, do_upload: bool = False, dry_run: bool = False) -> dict:
    data = _normalise(json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8")), source)
    if dry_run:
        parsed = {slug: recipe_photos.parse_entry(slug, v) for slug, v in data.items()}
        return {
            "ok": sum(1 for cols, _ in parsed.values() if cols is not None),
            "refused": [reason for cols, reason in parsed.values() if cols is None],
        }
    extra = await upload(data) if do_upload else {}
    async with transaction() as session:
        report = await recipe_photos.set_photos(session, OPERATOR, {"photos": data})  # type: ignore[arg-type]
    return {**report, **extra}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--source", choices=("ia", "photo"))
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args.file, args.source, do_upload=args.upload, dry_run=args.dry_run))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
