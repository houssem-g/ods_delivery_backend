"""Meta WhatsApp Cloud API webhook (port of base44/functions/whatsappWebhook).

    GET  ?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…
         → echoes hub.challenge when the token equals WHATSAPP_VERIFY_TOKEN (else 403).
    POST status callbacks (sent / delivered / read / failed), signed by Meta:
         X-Hub-Signature-256: sha256=<HMAC-SHA256(raw body, WHATSAPP_APP_SECRET)>
         → updates the outbound_messages row (by provider_message_id); on "failed" the
           SMS fallback for critical rows / numbers without WhatsApp.

Reachable at /api/webhooks/whatsapp and, like the Base44 function URL Meta may still
be configured with, /api/functions/whatsappWebhook. No session needed. Unsigned or
wrongly signed POSTs are refused (401); without WHATSAPP_APP_SECRET every POST is
refused (webhook OFF). Valid calls always answer 200 so Meta doesn't retry for 36 h.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.services import whatsapp

log = logging.getLogger("odsd.whatsapp")
router = APIRouter(tags=["webhooks"])
PATHS = ("/api/webhooks/whatsapp", "/api/functions/whatsappWebhook")


def hmac_sha256_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac_sha256_hex(app_secret, raw_body)
    return hmac.compare_digest(expected, header[len("sha256=") :].strip().lower())


def verify_challenge(params: dict[str, str], verify_token: str | None) -> str | None:
    if not verify_token or params.get("hub.mode") != "subscribe":
        return None
    if not hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
        return None
    return params.get("hub.challenge")


async def challenge(request: Request) -> Response:
    answer = verify_challenge(dict(request.query_params), settings.WHATSAPP_VERIFY_TOKEN)
    if answer is None:
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(answer, status_code=200)


async def statuses(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("x-hub-signature-256"), settings.WHATSAPP_APP_SECRET):
        return PlainTextResponse("Invalid signature", status_code=401)
    try:
        result = await whatsapp.apply_statuses(session, whatsapp.extract_statuses(json.loads(raw)))
        await session.commit()
    except Exception:
        # Still 200: a payload we can't process won't get better on retry.
        await session.rollback()
        log.exception("whatsapp webhook: payload not processed")
        return JSONResponse({"success": False})
    return JSONResponse({"success": True, **result})


for _path in PATHS:
    router.add_api_route(_path, challenge, methods=["GET"], include_in_schema=_path == PATHS[0])
    router.add_api_route(_path, statuses, methods=["POST"], include_in_schema=_path == PATHS[0])
