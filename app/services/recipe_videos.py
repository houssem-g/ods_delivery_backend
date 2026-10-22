"""Recipe videos (22/10/2026, owner: « des vidéos liées aux étapes, la préparation n'est pas toujours
claire »; decision 22/10: options 2 + 1, pilot on 5 recipes and 10 gestures).

(1) A passage of an existing YouTube video per step (`recipe_step_videos`): the video id, start / end in
seconds, its title and channel. The app plays it with YouTube's OWN player (youtube-nocookie embed, start /
end, muted) and only after a tap — nothing is copied, nothing is loaded before the tap.

(2) A library of GESTURES (`recipe_gestures`): 5–15 s clips of the basic gestures (« faire revenir
l'oignon », « rouler le couscous », « plier une brik »…). A clip of ours (free licence, no sound) lives on
the PUBLIC part of the bucket, `public/recipes/gestures/<key>.webm` with the `.mp4` (Safari) and the `.webp`
poster next to it; a gesture without a good free clip is a YouTube passage too. Each gesture carries
trigger words fr / ar and is linked AUTOMATICALLY to every step whose text says it (`link`), no list per
recipe: a new recipe gets its gestures for free.

A trigger is space-separated tokens, ALL of which must be in the step (any order); a token is alternatives
separated by « | »; « x* » = a word starting with x, « *x* » = a word containing x, « x » = the word x; a
token starting with « ! » must NOT be in the step (23/10, QA v3 n° 8: « !pate*|petri* » keeps « fariner et
paner » off a noodle dough). Words are folded like the recipe search (no case, no accent, Arabic letters
unified, harakat removed).

The file (data/recipes/videos.json) is read by scripts/import_recipe_videos.py or the admin function
importRecipeVideos: `{gestures: [...], passages: {slug: [{step, youtube_id, start, end, title, channel}]}}`.
Idempotent: upsert by key / (recipe, step); a recipe's passages not in the file are removed (the file is the
truth for the recipes it names); `prune: true` also removes the gestures absent from the file.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import AuditLog, Recipe, RecipeGesture, RecipeStepVideo
from app.security.deps import CurrentUser
from app.services import text_norm
from app.storage import s3

GESTURE_PREFIX = "public/recipes/gestures/"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
CLIP_RE = re.compile(r"^public/recipes/gestures/[a-z0-9][a-z0-9-]{1,59}\.webm$")
YOUTUBE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_PASSAGE_S = 600
MAX_STEP = 60
MAX_GESTURES_PER_STEP = 2
MAX_TRIGGERS = 30
TEXT_MAX = 160
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})


_LIGATURES = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae"})


def fold_words(text: Any) -> list[str]:
    """A step's words, folded like the recipe search (recipes.fold): « œufs » = « oeufs »."""
    return [w.translate(_AR_FOLD) for w in text_norm.tokenize(str(text or "").translate(_LIGATURES))]


def _token_ok(token: str, words: list[str]) -> bool:
    for alt in token.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        if alt.startswith("*") and alt.endswith("*") and len(alt) > 2:
            core = fold_words(alt[1:-1])
            if core and any(core[0] in w for w in words):
                return True
        elif alt.endswith("*") and len(alt) > 1:
            core = fold_words(alt[:-1])
            if core and any(w.startswith(core[0]) for w in words):
                return True
        else:
            core = fold_words(alt)
            if core and core[0] in words:
                return True
    return False


def matches(trigger: str, text: Any) -> bool:
    """Every token of the trigger is in the text, and none of its « !… » tokens (see the module doc)."""
    words = fold_words(text)
    tokens = [t for t in str(trigger).split() if t.strip("|!")]
    wanted = [t for t in tokens if not t.startswith("!")]
    banned = [t[1:] for t in tokens if t.startswith("!")]
    return (
        bool(wanted)
        and bool(words)
        and all(_token_ok(t, words) for t in wanted)
        and not any(_token_ok(t, words) for t in banned)
    )


def _clean(value: Any, n: int = TEXT_MAX) -> str | None:
    text = " ".join(str(value).split())[:n] if isinstance(value, (str, int, float)) else ""
    return text or None


def _https(value: Any) -> str | None:
    url = _clean(value, 500)
    return url if url and url.startswith("https://") else None


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n


def _seconds(value: Any) -> int | None:
    """12, "12", "1:05" or "0:01:05" → seconds."""
    if isinstance(value, str) and ":" in value:
        parts = value.strip().split(":")
        if not all(p.isdigit() for p in parts) or len(parts) > 3:
            return None
        n = 0
        for p in parts:
            n = n * 60 + int(p)
        return n
    return _int(value)


def _passage(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    vid = str(entry.get("youtube_id") or "").strip()
    if not YOUTUBE_RE.match(vid):
        return None, f"identifiant YouTube invalide « {vid} »"
    start, end = _seconds(entry.get("start")), _seconds(entry.get("end"))
    if start is None or end is None or start < 0 or end <= start or end - start > MAX_PASSAGE_S:
        return None, f"{vid} : début / fin invalides ({entry.get('start')} → {entry.get('end')})"
    return {"youtube_id": vid, "start_s": start, "end_s": end}, None


def _triggers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [" ".join(str(t).split())[:200] for t in value[:MAX_TRIGGERS] if isinstance(t, str) and t.strip()]
    return out


def parse_gesture(entry: Any, position: int = 0) -> tuple[dict[str, Any] | None, str | None]:
    """One gesture of the file → the row's columns, or a refusal."""
    if not isinstance(entry, dict):
        return None, "geste : un objet attendu"
    key = str(entry.get("key") or "").strip()
    if not KEY_RE.match(key):
        return None, f"geste : clé invalide « {key} »"
    name_fr, name_ar = _clean(entry.get("name_fr")), _clean(entry.get("name_ar"))
    if not name_fr or not name_ar:
        return None, f"{key} : nom fr et ar obligatoires"
    raw_credit = entry.get("credit") if isinstance(entry.get("credit"), dict) else {}
    credit = {
        k: v
        for k, v in {
            "title": _clean(raw_credit.get("title")),
            "channel": _clean(raw_credit.get("channel")),
            "author": _clean(raw_credit.get("author")),
            "license": _clean(raw_credit.get("license"), 80),
            "license_url": _https(raw_credit.get("license_url")),
            "source_url": _https(raw_credit.get("source_url")),
        }.items()
        if v
    } or None
    cols: dict[str, Any] = {
        "key": key,
        "name_fr": name_fr,
        "name_ar": name_ar,
        "clip_key": None,
        "youtube_id": None,
        "start_s": None,
        "end_s": None,
        "credit": credit,
        "triggers_fr": _triggers(entry.get("triggers_fr")),
        "triggers_ar": _triggers(entry.get("triggers_ar")),
        "position": position,
    }
    if entry.get("youtube_id"):
        passage, reason = _passage(entry)
        if passage is None:
            return None, f"{key} : {reason}"
        return {**cols, "kind": "youtube", **passage}, None
    clip = str(entry.get("clip") or f"{GESTURE_PREFIX}{key}.webm").strip()
    if not CLIP_RE.match(clip):
        return None, f"{key} : clip hors de {GESTURE_PREFIX}*.webm"
    return {**cols, "kind": "clip", "clip_key": clip}, None


def parse_passage(slug: str, entry: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(entry, dict):
        return None, f"{slug} : un objet attendu"
    step = _int(entry.get("step"))
    if step is None or not 1 <= step <= MAX_STEP:
        return None, f"{slug} : étape invalide « {entry.get('step')} »"
    passage, reason = _passage(entry)
    if passage is None:
        return None, f"{slug} étape {step} : {reason}"
    return {
        "step": step,
        **passage,
        "title": _clean(entry.get("title"), 200),
        "channel": _clean(entry.get("channel")),
    }, None


async def import_videos(
    session: AsyncSession, data: Any, *, prune: bool = False, actor: uuid.UUID | None = None
) -> dict[str, Any]:
    """Upserts the gestures and the passages of a videos file (see the module doc). Idempotent."""
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "videos: {gestures, passages}")
    report: dict[str, Any] = {
        "success": True,
        "gestures": {"created": 0, "updated": 0, "unchanged": 0, "removed": 0},
        "passages": {"created": 0, "updated": 0, "unchanged": 0, "removed": 0},
        "unknown_recipes": [],
        "refused": [],
    }
    gestures_in = data.get("gestures") if isinstance(data.get("gestures"), list) else []
    existing = {g.key: g for g in (await session.execute(select(RecipeGesture))).scalars()}
    seen: set[str] = set()
    for i, entry in enumerate(gestures_in[:500]):
        cols, reason = parse_gesture(entry, i)
        if cols is None:
            report["refused"].append(reason)
            continue
        seen.add(cols["key"])
        row = existing.get(cols["key"])
        if row is None:
            session.add(RecipeGesture(**cols))
            report["gestures"]["created"] += 1
        elif all(getattr(row, k) == v for k, v in cols.items()):
            report["gestures"]["unchanged"] += 1
        else:
            for k, v in cols.items():
                setattr(row, k, v)
            report["gestures"]["updated"] += 1
    if prune:
        for key, row in existing.items():
            if key not in seen:
                await session.delete(row)
                report["gestures"]["removed"] += 1

    passages_in = data.get("passages") if isinstance(data.get("passages"), dict) else {}
    slugs = [str(s) for s in list(passages_in)[:2000]]
    recipes = {
        r.slug: r.id
        for r in (await session.execute(select(Recipe.slug, Recipe.id).where(Recipe.slug.in_(slugs)))).all()
    }
    for slug in slugs:
        rid = recipes.get(slug)
        if rid is None:
            report["unknown_recipes"].append(slug)
            continue
        entries = passages_in[slug] if isinstance(passages_in[slug], list) else []
        current = {
            v.step: v
            for v in (
                await session.execute(select(RecipeStepVideo).where(RecipeStepVideo.recipe_id == rid))
            ).scalars()
        }
        wanted: dict[int, dict[str, Any]] = {}
        for entry in entries[:MAX_STEP]:
            cols, reason = parse_passage(slug, entry)
            if cols is None:
                report["refused"].append(reason)
                continue
            wanted[cols["step"]] = cols
        for step, cols in wanted.items():
            row = current.get(step)
            if row is None:
                session.add(RecipeStepVideo(recipe_id=rid, **cols))
                report["passages"]["created"] += 1
            elif all(getattr(row, k) == v for k, v in cols.items()):
                report["passages"]["unchanged"] += 1
            else:
                for k, v in cols.items():
                    setattr(row, k, v)
                report["passages"]["updated"] += 1
        gone = [s for s in current if s not in wanted]
        if gone:
            stale = (RecipeStepVideo.recipe_id == rid) & RecipeStepVideo.step.in_(gone)
            await session.execute(delete(RecipeStepVideo).where(stale))
            report["passages"]["removed"] += len(gone)
    changed = sum(report["gestures"][k] + report["passages"][k] for k in ("created", "updated", "removed"))
    if changed:
        session.add(
            AuditLog(
                actor_user_id=actor,
                action="recipes.videos",
                entity="recipe_videos",
                entity_id="videos.json",
                before=None,
                after={"gestures": report["gestures"], "passages": report["passages"]},
            )
        )
    await session.flush()
    report["unknown_recipes"] = report["unknown_recipes"][:200]
    report["refused"] = report["refused"][:200]
    return report


async def import_admin(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """importRecipeVideos: an admin sends the videos file (or {videos: file, prune?})."""
    if admin is None or not admin.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    data = payload.get("videos") if isinstance(payload.get("videos"), dict) else payload
    return await import_videos(session, data, prune=bool(payload.get("prune")), actor=admin.id)


# --- reading -------------------------------------------------------------------------------------------


def _gesture_doc(g: RecipeGesture) -> dict[str, Any]:
    doc: dict[str, Any] = {"key": g.key, "name_fr": g.name_fr, "name_ar": g.name_ar, "kind": g.kind,
                           "credit": g.credit or None}  # fmt: skip
    if g.kind == "clip" and g.clip_key:
        base = g.clip_key[: -len(".webm")]
        doc["clip"] = {
            "webm": s3.public_url(g.clip_key),
            "mp4": s3.public_url(f"{base}.mp4"),
            "poster": s3.public_url(f"{base}.webp"),
        }
    else:
        doc["youtube"] = {"id": g.youtube_id, "start": g.start_s, "end": g.end_s}
    return doc


def link(
    steps_fr: list[Any],
    steps_ar: list[Any],
    gestures: Iterable[RecipeGesture],
    skip: dict[int, str] | None = None,
) -> list[list[RecipeGesture]]:
    """The gestures of each step (at most MAX_GESTURES_PER_STEP, in the library's order): a gesture is linked
    when one of its fr triggers matches the French step OR one of its ar triggers the Arabic one. `skip`
    (step index → YouTube id): a YouTube gesture from the same video as the step's own passage is left out."""
    ordered = sorted(gestures, key=lambda g: (g.position, g.key))
    out: list[list[RecipeGesture]] = []
    for k in range(max(len(steps_fr), len(steps_ar))):
        fr = steps_fr[k] if k < len(steps_fr) else ""
        ar = steps_ar[k] if k < len(steps_ar) else ""
        found = [
            g
            for g in ordered
            if any(matches(t, fr) for t in g.triggers_fr or [])
            or any(matches(t, ar) for t in g.triggers_ar or [])
        ]
        same = (skip or {}).get(k)
        found = [g for g in found if not (same and g.youtube_id == same)]
        out.append(found[:MAX_GESTURES_PER_STEP])
    return out


async def step_media(session: AsyncSession, recipe: Recipe) -> list[dict[str, Any] | None]:
    """getRecipe's `step_media`: one entry per step (null when nothing): {video: passage | null, gestures}."""
    passages = {
        v.step: v
        for v in (
            await session.execute(select(RecipeStepVideo).where(RecipeStepVideo.recipe_id == recipe.id))
        ).scalars()
    }
    gestures = list((await session.execute(select(RecipeGesture))).scalars())
    steps_fr, steps_ar = list(recipe.steps_fr or []), list(recipe.steps_ar or [])
    skip = {s - 1: v.youtube_id for s, v in passages.items()}
    linked = link(steps_fr, steps_ar, gestures, skip)
    out: list[dict[str, Any] | None] = []
    for k, found in enumerate(linked):
        v = passages.get(k + 1)
        video = (
            {"id": v.youtube_id, "start": v.start_s, "end": v.end_s, "title": v.title, "channel": v.channel}
            if v
            else None
        )
        out.append({"video": video, "gestures": [_gesture_doc(g) for g in found]} if video or found else None)
    return out


def coverage(recipes: Iterable[dict[str, Any]], gestures: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts what the gestures of a file cover in a list of recipes {slug, steps_fr, steps_ar}: recipes with
    at least one linked step, steps linked, and per gesture the number of recipes and steps."""
    rows = []
    for i, entry in enumerate(gestures):
        cols, _ = parse_gesture(entry, i)
        if cols:
            rows.append(RecipeGesture(**cols))
    per: dict[str, dict[str, int]] = {g.key: {"recipes": 0, "steps": 0} for g in rows}
    n_recipes = n_steps = covered_recipes = covered_steps = 0
    for r in recipes:
        n_recipes += 1
        linked = link(list(r.get("steps_fr") or []), list(r.get("steps_ar") or []), rows)
        n_steps += len(linked)
        hit = [found for found in linked if found]
        covered_steps += len(hit)
        covered_recipes += bool(hit)
        keys: set[str] = set()
        for found in linked:
            for g in found:
                per[g.key]["steps"] += 1
                keys.add(g.key)
        for key in keys:
            per[key]["recipes"] += 1
    return {
        "recipes": n_recipes,
        "steps": n_steps,
        "recipes_with_a_gesture": covered_recipes,
        "steps_with_a_gesture": covered_steps,
        "per_gesture": per,
    }
