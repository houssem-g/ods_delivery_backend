"""Record the videos of the Recettes (YouTube passages per step + the gestures library) — idempotent.

    uv run python -m scripts.import_recipe_videos data/recipes/videos.json [--upload DIR] [--prune]
    uv run python -m scripts.import_recipe_videos data/recipes/videos.json --dry-run
    uv run python -m scripts.import_recipe_videos data/recipes/videos.json --coverage [recettes*.json]

The file: {"gestures": [{key, name_fr, name_ar, clip? | youtube_id + start + end, credit, triggers_fr,
triggers_ar}], "passages": {"<slug>": [{step, youtube_id, start, end, title, channel}]}} — same rules as
the admin function importRecipeVideos (app/services/recipe_videos.py).
--upload DIR: first puts DIR/<key>.webm, .mp4 and .webp of each « clip » gesture at
public/recipes/gestures/ (public ACL, cached a year), then records; a gesture whose WebM is missing in DIR
is reported and not recorded.
--dry-run: checks the file, prints the refusals, writes nothing.
--coverage: counts, without the database, how many steps and recipes of the recipe files the gestures
reach (the « généraliser » figure).
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.db import transaction
from app.services import recipe_videos
from app.storage import s3

CACHE = "public, max-age=31536000, immutable"
TYPES = {".webm": "video/webm", ".mp4": "video/mp4", ".webp": "image/webp"}


def _recipes(paths: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("recettes") if isinstance(data, dict) else data
        for r in items or []:
            if isinstance(r, dict):
                out.append(
                    {
                        "slug": r.get("id") or r.get("slug"),
                        "steps_fr": r.get("etapes_fr") or r.get("steps_fr"),
                        "steps_ar": r.get("etapes_ar") or r.get("steps_ar"),
                    }
                )
    return out


async def upload(data: dict[str, Any], folder: Path) -> dict[str, Any]:
    """Puts the files of the « clip » gestures in the bucket; drops the gestures whose WebM is missing."""
    report: dict[str, Any] = {"uploaded": 0, "missing_file": []}
    kept = []
    for entry in data.get("gestures") or []:
        if not isinstance(entry, dict) or entry.get("youtube_id"):
            kept.append(entry)
            continue
        key = str(entry.get("key") or "")
        if not await asyncio.to_thread((folder / f"{key}.webm").is_file):
            report["missing_file"].append(key)
            continue
        for ext, ctype in TYPES.items():
            file = folder / f"{key}{ext}"
            if await asyncio.to_thread(file.is_file):
                body = await asyncio.to_thread(file.read_bytes)
                target = f"{recipe_videos.GESTURE_PREFIX}{key}{ext}"
                await s3.put_object(target, body, ctype, cache_control=CACHE)
                report["uploaded"] += 1
        kept.append(entry)
    data["gestures"] = kept
    return report


async def run(
    path: Path, *, prune: bool = False, dry_run: bool = False, upload_dir: Path | None = None
) -> dict[str, Any]:
    data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    if dry_run:
        gestures = data.get("gestures") or []
        passages = data.get("passages") or {}
        refused = [r for _, r in (recipe_videos.parse_gesture(g, i) for i, g in enumerate(gestures)) if r]
        for slug, entries in passages.items():
            refused += [r for _, r in (recipe_videos.parse_passage(slug, e) for e in entries) if r]
        n_passages = sum(len(v) for v in passages.values())
        return {"gestures": len(gestures), "passages": n_passages, "refused": refused}
    extra = await upload(data, upload_dir) if upload_dir else {}
    async with transaction() as session:
        report = await recipe_videos.import_videos(session, data, prune=prune)
    return {**report, **extra}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--upload", type=Path, metavar="DIR")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--coverage", nargs="*", type=Path)
    args = parser.parse_args()
    if args.coverage is not None:
        files = args.coverage or sorted(Path("data/recipes").glob("recettes*.json"))
        data = json.loads(args.file.read_text(encoding="utf-8"))
        report = recipe_videos.coverage(_recipes(files), data.get("gestures") or [])
    else:
        report = asyncio.run(run(args.file, prune=args.prune, dry_run=args.dry_run, upload_dir=args.upload))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
