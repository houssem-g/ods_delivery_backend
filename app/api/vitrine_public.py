"""GET /api/public/vitrine/merchant/{token} — the shop owner's private page (Vitrine, contract §8).
GET /api/public/vitrine/thumb/{product_id} — a dish's 256 px WebP thumbnail (QA 19/10 M15,
app/services/vitrine_thumbs.py): made on the first request and answered as image/webp, then a redirect
to the bucket; the original photo (redirect) when it can't be made. No account: the menu is public.

No account: the 32-byte token of the link IS the access (found by sha256; « Nouveau lien » in the
admin kills the old one). 60 reads per hour per token and per address. Never indexed. The answer
holds counters only: no customer name, phone or address.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, transaction
from app.errors import ApiError
from app.models import VitrineProduct
from app.rate_limit import allow
from app.services import vitrine_stats, vitrine_thumbs
from app.services.vitrine import token_hash
from app.storage import s3

log = logging.getLogger("odsd.vitrine_public")

router = APIRouter(prefix="/api/public/vitrine", tags=["vitrine"])
RATE = "60/hour"
NO_INDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


@router.get("/merchant/{token}")
async def merchant(
    token: str,
    request: Request,
    month: str | None = Query(default=None, max_length=7),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    key = token_hash(token)[:32]
    if not allow("vitrine_merchant", key, RATE) or not allow(
        "vitrine_merchant_ip", get_remote_address(request), RATE
    ):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    body: dict[str, Any] = await vitrine_stats.merchant_stats(session, token, month)
    await session.commit()  # the « ouvert N fois » counter
    return JSONResponse(body, headers=NO_INDEX)


THUMB_RATE = "600/hour"  # thumbnails MADE per address (served ones are not counted)


@router.get("/thumb/{product_id}")
async def thumb(product_id: uuid.UUID, request: Request) -> Response:
    async with transaction() as session:
        product = await session.get(VitrineProduct, product_id)
        photo_key = product.photo_key if product is not None else None
        made_key = product.photo_thumb_key if product is not None else None
    if not photo_key:
        raise ApiError(404, "not_found", "No photo")
    key = vitrine_thumbs.thumb_key(photo_key)
    cached = {"Cache-Control": vitrine_thumbs.CACHE_CONTROL}
    if made_key == key or await vitrine_thumbs.remembered(photo_key):
        return RedirectResponse(
            s3.public_url(key), status_code=302, headers={"Cache-Control": "public, max-age=86400"}
        )
    original = vitrine_thumbs.original_url(photo_key) or ""
    fallback = RedirectResponse(original, status_code=302, headers={"Cache-Control": "no-store"})
    if not allow("vitrine_thumb_ip", get_remote_address(request), THUMB_RATE):
        return fallback
    try:
        webp = await vitrine_thumbs.make(photo_key)
    except (vitrine_thumbs.ThumbFailed, s3.StorageUnavailable) as exc:
        log.warning("vitrine thumbnail not made for %s: %s", product_id, exc)
        return fallback
    except Exception:
        log.exception("vitrine thumbnail failed for %s", product_id)
        return fallback
    return Response(webp, media_type="image/webp", headers=cached)
