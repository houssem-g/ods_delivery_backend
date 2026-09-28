"""getCourierIdPhotos — admin only: 5-minute links to couriers' ID document photos
(base44/functions/getCourierIdPhotos). The private key itself is never answered.

Body: { courier_ids: string[] } (at most 50). Returns { success, photos: { [courier_id]:
{ url, private: true, expires_at } } } — couriers without a photo are absent.
"""

import logging
from datetime import timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.compat.dates import legacy_datetime
from app.models import Courier
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.storage import s3

log = logging.getLogger("odsd.functions")
SIGNED_URL_SECONDS = 300
MAX_IDS = 50


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not user.is_admin:
        return 403, {"error": "Forbidden"}
    raw = payload.get("courier_ids")
    if not isinstance(raw, list):
        return 400, {"error": "courier_ids required"}
    ids = []
    for value in raw:
        uid = as_uuid(value) if isinstance(value, str) and 0 < len(value) <= 64 else None
        if uid is not None and uid not in ids:
            ids.append(uid)
    ids = ids[:MAX_IDS]
    photos: dict[str, dict[str, Any]] = {}
    if not ids:
        return 200, {"success": True, "photos": photos}
    rows = (
        await session.execute(select(Courier.id, Courier.id_document_key).where(Courier.id.in_(ids)))
    ).all()
    expires_at = legacy_datetime(ot.now_utc() + timedelta(seconds=SIGNED_URL_SECONDS)) + "Z"
    for courier_id, key in rows:
        if not key or not key.startswith("private/"):
            continue
        try:
            url = s3.presign_get(key, SIGNED_URL_SECONDS)
        except Exception as exc:  # signing is local; a broken config must not hide the others
            log.error("getCourierIdPhotos: signing failed for %s: %s", courier_id, type(exc).__name__)
            continue
        photos[str(courier_id)] = {"url": url, "private": True, "expires_at": expires_at}
    return 200, {"success": True, "photos": photos}
