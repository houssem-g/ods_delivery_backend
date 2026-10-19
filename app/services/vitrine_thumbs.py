"""Vitrine dish thumbnails (QA 19/10/2026 M15): the menu showed every dish's ORIGINAL photo (ODS sends
992×992 PNGs, 0.8 to 1.6 MB each, served as binary/octet-stream) in a 64 px square — 38 MB on Android,
108 MB on an iPhone per visit.

Each dish photo gets ONE 256 px WebP thumbnail (≈ 10-25 kB, Content-Type image/webp, cached a week),
stored in our bucket under `public/vitrine-thumbs/<sha256(photo)>-256.webp` and remembered on the dish
(`vitrine_products.photo_thumb_key`). The key is derived from the photo itself: a dish whose photo
changes gets a new thumbnail, dishes sharing a photo share one.

- getVitrine / listVitrinesNearby give each dish `thumb_url` next to `photo_url` (kept for the dish
  sheet): the bucket URL once the thumbnail exists, else `GET /api/public/vitrine/thumb/{product_id}?v=…`.
- That route makes it on the first request (read the original — our bucket by key, or the ODS https
  URL through `fetch_source`: public addresses only, no redirect, 15 MB and 10 s at most —, resize in a
  thread, store, remember) and answers the WebP itself; afterwards it redirects to the bucket. Never more
  than MAX_PARALLEL at once per process, and no database connection is held while it works.
- When the original can't be read or isn't an image, the route redirects to the original photo (the menu
  still shows something) and nothing is remembered: a later request tries again.
"""

import asyncio
import hashlib
import io
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps
from sqlalchemy import select, update

from app.config import settings
from app.db import transaction
from app.models import VitrineProduct
from app.services import shops as shop_service
from app.storage import s3

log = logging.getLogger("odsd.vitrine_thumbs")

THUMB_PX = 256
WEBP_QUALITY = 72
PREFIX = "public/vitrine-thumbs/"
MAX_SOURCE_BYTES = 15 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000  # 6300 × 6300: a bigger « photo » is refused (decompression bomb)
FETCH_TIMEOUT_SECONDS = 10.0
MAX_PARALLEL = 2  # thumbnails made at once per process (memory: one decoded photo ≈ 4 MB)
CACHE_CONTROL = "public, max-age=604800"  # the key changes with the photo: a week is safe
VERSION_LEN = 8

Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
_gate = asyncio.Semaphore(MAX_PARALLEL)
_transport: httpx.AsyncBaseTransport | None = None  # tests replace the network


class ThumbFailed(Exception):
    pass


def thumb_key(photo_key: str) -> str:
    return f"{PREFIX}{hashlib.sha256(photo_key.encode()).hexdigest()[:40]}-{THUMB_PX}.webp"


def thumb_url(product: VitrineProduct) -> str | None:
    """What the menu shows in its small squares (photo_url stays the original, for the dish sheet)."""
    if not product.photo_key:
        return None
    key = thumb_key(product.photo_key)
    if product.photo_thumb_key == key:
        return s3.public_url(key)
    version = key[len(PREFIX) : len(PREFIX) + VERSION_LEN]
    return f"{settings.API_PUBLIC_URL.rstrip('/')}/api/public/vitrine/thumb/{product.id}?v={version}"


async def host_is_public(host: str, port: int) -> bool:
    """Every address the name resolves to is a public one (no loopback, private, link-local, metadata)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


async def fetch_source(url: str) -> bytes:
    """An https photo of another service (ODS), read with care: public hosts only, no redirect, size and
    time bounded."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ThumbFailed("not an https URL")
    if not await host_is_public(parts.hostname, parts.port or 443):
        raise ThumbFailed("host not public")
    try:
        return await _download(url)
    except httpx.HTTPError as exc:
        raise ThumbFailed(f"source unreachable: {type(exc).__name__}") from exc


async def _download(url: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async with (
        httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, transport=_transport
        ) as client,
        client.stream("GET", url) as answer,
    ):
        if answer.status_code != 200:
            raise ThumbFailed(f"source answered {answer.status_code}")
        async for chunk in answer.aiter_bytes():
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ThumbFailed("source too large")
            chunks.append(chunk)
    return b"".join(chunks)


async def source_bytes(photo_key: str) -> bytes:
    if photo_key.startswith(("https://", "http://")):
        return await fetch_source(photo_key)
    try:
        return await s3.get_object(photo_key, MAX_SOURCE_BYTES)  # one of our own uploads
    except Exception as exc:
        raise ThumbFailed(f"bucket read failed: {exc}") from exc


def render(data: bytes) -> bytes:
    """The photo → a WebP fitting THUMB_PX × THUMB_PX (aspect kept, EXIF orientation applied)."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ThumbFailed("image too large")
            source.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # JPEG: decode at a reduced scale
            image = ImageOps.exif_transpose(source) or source
            image.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
            transparent = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            image = image.convert("RGBA" if transparent else "RGB")
            out = io.BytesIO()
            image.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
            return out.getvalue()
    except ThumbFailed:
        raise
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise ThumbFailed(f"not a readable image: {exc}") from exc


async def make(photo_key: str) -> bytes:
    """Makes, stores and remembers the thumbnail of this dish photo; returns the WebP bytes."""
    key = thumb_key(photo_key)
    async with _gate:
        webp = await asyncio.to_thread(render, await source_bytes(photo_key))
        await s3.put_object(key, webp, "image/webp", cache_control=CACHE_CONTROL)
    async with transaction() as session:  # short: no connection held while fetching / resizing
        await session.execute(
            update(VitrineProduct).where(VitrineProduct.photo_key == photo_key).values(photo_thumb_key=key)
        )
    return webp


async def remembered(photo_key: str) -> bool:
    """Another dish with the same photo already has the thumbnail: this one shares it."""
    key = thumb_key(photo_key)
    async with transaction() as session:
        found = (
            await session.execute(
                select(VitrineProduct.id).where(VitrineProduct.photo_thumb_key == key).limit(1)
            )
        ).first()
        if found is None:
            return False
        await session.execute(
            update(VitrineProduct).where(VitrineProduct.photo_key == photo_key).values(photo_thumb_key=key)
        )
    return True


def original_url(photo_key: str) -> str | None:
    return shop_service.photo_url_of(photo_key)
