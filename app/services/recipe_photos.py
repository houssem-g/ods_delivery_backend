"""Recipe photos (22/10/2026, owner: the Recettes redesign WITH dish photos): free-licence photos found
online first (Openverse / Wikimedia Commons: CC0, public domain, CC BY, CC BY-SA), AI pictures (≤ 100) for the
recipes without a good free one.

The files are WebP in the PUBLIC part of the ODS Delivery bucket, under `public/recipes/` — the same public
path as the Vitrine thumbnails (`public/vitrine-thumbs/`, app/services/vitrine_thumbs.py): readable without a
signature at `s3.public_url(key)` (the FILES_ORIGIN the app's Content-Security-Policy already allows), public
ACL on upload (`S3_PUBLIC_OBJECT_ACL`). Convention: `public/recipes/<slug>.webp` (~960 px, the recipe sheet)
and `public/recipes/<slug>-480.webp` (~480 px, the cards); the small one is optional.

Only the KEYS are stored (`recipes.photo_key`, `photo_thumb_key`): the URL is built when read, so a change of
public origin (CDN) needs no data change. An admin records them with setRecipePhoto, one recipe or a whole
JSON file `{slug: url | key | {photo, thumb, alt_fr, alt_ar, source} | null}` (null = remove the photo). A URL
is accepted only on our own public origin, a key only under `public/recipes/` and ending in .webp: no other
host can end up in an <img> of the app.

The photo team's file (photos/libres/photos_<x>.json) is accepted as it is: `{slug: {file, source_page,
image_url, author, license, license_url, title, note, score}}` → only a photo scored 5/5 is kept (owner
21/10: beautiful photos or nothing; an entry without a score is accepted, a lower score refused) → the
key `public/recipes/<slug>.webp`, source 'photo', and the CREDIT kept (`photo_credit`: author, licence,
links). `credit_required` is true for CC BY / BY-SA (the app then shows « © author · licence » in small,
with the link), false for CC0 / public domain.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import AuditLog, Recipe
from app.models.recipes import PHOTO_SOURCES
from app.security.deps import CurrentUser
from app.storage import s3

PREFIX = "public/recipes/"
KEY_RE = re.compile(r"^public/recipes/[a-z0-9][a-z0-9_-]{0,150}\.webp$")
ALT_MAX = 200
CREDIT_MAX = 200
TOP_SCORE = 5  # owner 21/10: only the photos scored 5/5 by the photo team
# free photos are « photo »; the photo team's words are accepted too
SOURCE_ALIASES = {
    **dict.fromkeys(("libre", "free", "openverse", "wikimedia", "commons"), "photo"),
    **dict.fromkeys(("gemini", "ai", "fal"), "ia"),
}
NO_CREDIT_LICENSES = ("cc0", "pdm", "public domain", "domaine public")
MAX_PHOTOS = 1000  # one JSON file of the whole catalogue (547 recipes on 21/10)


def default_key(slug: str) -> str:
    return f"{PREFIX}{slug}.webp"


def to_key(value: Any) -> str | None:
    """A bucket key under public/recipes/ (*.webp) from a key or a URL of our public origin, else None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    base = settings.public_files_base_url.rstrip("/") + "/"
    if raw.startswith(("http://", "https://")):
        if not raw.startswith(base):
            return None
        raw = raw[len(base) :]
    raw = raw.split("?", 1)[0].lstrip("/")
    if raw.startswith("recipes/"):
        raw = f"public/{raw}"
    return raw if KEY_RE.match(raw) else None


def fields(recipe: Recipe) -> dict[str, Any]:
    """What a card / the recipe sheet shows: photo URLs (None without a photo), alt texts, source."""
    if not recipe.photo_key:
        return {
            "photo_url": None,
            "photo_thumb_url": None,
            "photo_alt_fr": None,
            "photo_alt_ar": None,
            "photo_source": None,
            "photo_credit": None,
        }
    return {
        "photo_url": s3.public_url(recipe.photo_key),
        "photo_thumb_url": s3.public_url(recipe.photo_thumb_key) if recipe.photo_thumb_key else None,
        "photo_alt_fr": recipe.photo_alt_fr or recipe.title_fr,
        "photo_alt_ar": recipe.photo_alt_ar or recipe.title_ar,
        "photo_source": recipe.photo_source,
        "photo_credit": recipe.photo_credit or None,
    }


def _clean(value: Any, n: int = CREDIT_MAX) -> str | None:
    text = " ".join(str(value).split())[:n] if isinstance(value, str) else ""
    return text or None


def _https(value: Any) -> str | None:
    """A credit link: http(s) only (it is an <a href>, never javascript: or data:)."""
    url = _clean(value, 500)
    return url if url and url.startswith(("https://", "http://")) else None


def credit_of(entry: dict[str, Any]) -> dict[str, Any] | None:
    """{author, license, license_url, source_url, title, credit_required} from an entry, None without one."""
    raw = entry.get("credit") if isinstance(entry.get("credit"), dict) else entry
    author = _clean(raw.get("author") or raw.get("auteur"))
    license_ = _clean(raw.get("license") or raw.get("licence"), 60)
    if not author and not license_:
        return None
    required = not (license_ and license_.lower().startswith(NO_CREDIT_LICENSES))
    return {
        "author": author,
        "license": license_,
        "license_url": _https(raw.get("license_url") or raw.get("licence_url")),
        "source_url": _https(raw.get("source_page") or raw.get("source_url") or raw.get("url_page")),
        "title": _clean(raw.get("title") or raw.get("titre")),
        "credit_required": required,
    }


def _is_top(score: Any) -> bool:
    """The photo team scores each photo out of 5; only a 5/5 is kept (« 5 », 5, « 5/5 »)."""
    try:
        return float(str(score).split("/")[0].replace(",", ".")) >= TOP_SCORE
    except ValueError:
        return False


def _alt(value: Any) -> str | None:
    text = " ".join(str(value).split())[:ALT_MAX] if isinstance(value, str) else ""
    return text or None


def parse_entry(slug: str, value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """One entry of a photo file → the columns to write (photo_key None = remove), or a refusal."""
    if value is None:
        return {"photo_key": None, "photo_thumb_key": None, "photo_alt_fr": None, "photo_alt_ar": None,
                "photo_source": None, "photo_credit": None, "photo_set_at": None}, None  # fmt: skip
    entry = value if isinstance(value, dict) else {"photo": value}
    score = entry.get("score")
    if score is not None and not _is_top(score):  # owner 21/10: beautiful photos or nothing
        return None, f"{slug}: photo notée {score}/5 (seules les 5/5 sont gardées)"
    given = entry.get("photo") or entry.get("url") or entry.get("photo_url")
    if not given and entry.get("file"):  # the photo team's file: uploaded as public/recipes/<slug>.webp
        given = default_key(slug)
    key = to_key(given)
    if key is None:
        return None, f"{slug}: photo hors de public/recipes/*.webp ou d'une autre origine"
    thumb_raw = entry.get("thumb") or entry.get("thumb_url") or entry.get("photo_thumb_url")
    thumb = to_key(thumb_raw) if thumb_raw else None
    if thumb_raw and thumb is None:
        return None, f"{slug}: miniature hors de public/recipes/*.webp ou d'une autre origine"
    credit = credit_of(entry)
    source = str(entry.get("source") or ("photo" if credit else "ia")).strip().lower()
    source = SOURCE_ALIASES.get(source, source)
    if source not in PHOTO_SOURCES:
        return None, f"{slug}: source « {source} » (ia ou photo)"
    return {
        "photo_key": key,
        "photo_thumb_key": thumb,
        "photo_alt_fr": _alt(entry.get("alt_fr") or entry.get("alt")),
        "photo_alt_ar": _alt(entry.get("alt_ar")),
        "photo_source": source,
        "photo_credit": credit,
        "photo_set_at": datetime.now(UTC),
    }, None


def _entries(payload: dict[str, Any]) -> dict[str, Any]:
    """{photos: {slug: …}} (a JSON file) or one recipe {slug | id, photo, thumb?, alt_*?, source?}."""
    photos = payload.get("photos")
    if isinstance(photos, dict):
        return dict(list(photos.items())[:MAX_PHOTOS])
    if isinstance(photos, list):  # [{slug, photo, …}]
        return {str(p.get("slug")): p for p in photos[:MAX_PHOTOS] if isinstance(p, dict) and p.get("slug")}
    ref = payload.get("slug") or payload.get("id")
    if not ref:
        raise ApiError(400, "invalid_request", "slug, id or photos")
    return {str(ref): None if payload.get("photo", "") is None else payload}


async def set_photos(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """setRecipePhoto: records (or removes) the photos; an audit row per recipe changed."""
    if admin is None or not admin.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    entries = _entries(payload)
    ids = [uuid.UUID(k) for k in entries if re.fullmatch(r"[0-9a-fA-F-]{36}", k)]
    rows = list(
        (
            await session.execute(
                select(Recipe).where((Recipe.slug.in_(list(entries))) | (Recipe.id.in_(ids)))
            )
        ).scalars()
    )
    by_ref = {r.slug: r for r in rows} | {str(r.id): r for r in rows}
    report: dict[str, Any] = {
        "success": True,
        "updated": 0,
        "cleared": 0,
        "unchanged": 0,
        "unknown": [],
        "refused": [],
    }
    for ref, value in entries.items():
        recipe = by_ref.get(ref)
        if recipe is None:
            report["unknown"].append(ref)
            continue
        cols, reason = parse_entry(recipe.slug, value)
        if cols is None:
            report["refused"].append(reason)
            continue
        same = all(getattr(recipe, k) == v for k, v in cols.items() if k != "photo_set_at")
        if same:
            report["unchanged"] += 1
            continue
        before = recipe.photo_key
        for k, v in cols.items():
            setattr(recipe, k, v)
        report["cleared" if cols["photo_key"] is None else "updated"] += 1
        session.add(
            AuditLog(
                actor_user_id=admin.id,
                action="recipes.photo",
                entity="recipes",
                entity_id=str(recipe.id),
                before={"photo_key": before},
                after={
                    "photo_key": cols["photo_key"],
                    "source": cols["photo_source"],
                    "license": (cols.get("photo_credit") or {}).get("license"),
                },
            )
        )
    await session.flush()
    report["unknown"] = report["unknown"][:200]
    report["refused"] = report["refused"][:200]
    return report
