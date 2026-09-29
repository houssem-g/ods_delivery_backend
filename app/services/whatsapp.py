"""WhatsApp template messages (Meta Cloud API) with an SMS fallback (WinSMS).

Port of base44/functions/sendWhatsAppMessage + the status part of whatsappWebhook.
OFF until the settings exist: without WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID
(WINSMS_API_KEY + WINSMS_SENDER for SMS) nothing leaves the server, an
`outbound_messages` row with status "disabled" is written and the call succeeds.
`MESSAGING_DISABLED=true` is the kill switch.

Entry points (all take the caller's session; the caller commits):
    send_template(...)     the Deno `send` action
    fallback(log_id)       WhatsApp row -> SMS (webhook "failed")
    check_pending(order)   SMS for critical WhatsApp not delivered in time + due retries
    summary(key)           what happened to a message (+ its SMS)
    apply_statuses(events) webhook status callbacks
They answer `(status_code, json)` exactly like the Deno actions.

Reliability: strict +216 mobile validation (anti SMS/WA pumping); a foreign mobile only when it
is the user's verified phone (or for the verification code itself, rate-limited upstream),
per-number and global limits, idempotency key (unique column), retries with backoff
on transient errors, SMS on immediate failure / webhook "failed" / critical message not
"delivered" within WHATSAPP_FALLBACK_SECONDS.
The HTTP calls run inside the caller's transaction (as on Base44); every call has a
timeout (MESSAGING_HTTP_TIMEOUT_SECONDS) and at most 3 WhatsApp / 2 SMS attempts.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import OutboundMessage, User
from app.security.tokens import now_utc
from app.services.phones import InvalidPhone, is_international_mobile, to_e164

log = logging.getLogger("odsd.whatsapp")

Lang = Literal["fr", "ar"]
ErrorClass = Literal["transient", "no_whatsapp", "permanent"]
Result = tuple[int, dict[str, Any]]


# ─────────────────────────── Templates ───────────────────────────


@dataclass(frozen=True)
class TemplateDef:
    setting: str  # settings attribute that can override the Meta template name
    default_name: str
    params: int
    requires_opt_in: bool  # needs users.whatsapp_opt_in_at (non-critical, marketing-ish)
    sms: Callable[[list[str], Lang], str] | None  # SMS text when WhatsApp can't deliver
    copy_code_button: bool = False  # authentication templates: the code as URL-button parameter


def _sms_no_response(p: list[str], lang: Lang) -> str:
    if lang == "ar":
        return f"ODS: المندوب {p[0]} أمام منزلك بطلبك ({p[1]}) ولا يستطيع الوصول إليك. اتصل به: {p[2]}"
    return (
        f"ODS: votre livreur {p[0]} est devant chez vous avec votre commande ({p[1]}) "
        f"et n'arrive pas à vous joindre. Appelez-le : {p[2]}"
    )


def _sms_code(p: list[str], lang: Lang) -> str:
    return f"ODS: رمز التحقق {p[0]}" if lang == "ar" else f"ODS: votre code de vérification est {p[0]}"


TEMPLATES: dict[str, TemplateDef] = {
    # Courier at the door, customer unreachable: transactional, sent without the opt-in.
    "customer_no_response": TemplateDef(
        "WHATSAPP_TPL_NO_RESPONSE", "ods_livreur_injoignable", 3, False, _sms_no_response
    ),
    "courier_on_the_way": TemplateDef("WHATSAPP_TPL_ON_THE_WAY", "ods_livreur_en_route", 2, True, None),
    "new_offer": TemplateDef("WHATSAPP_TPL_NEW_OFFER", "ods_nouvelle_offre", 2, True, None),
    "verification_code": TemplateDef(
        "WHATSAPP_TPL_VERIFICATION", "ods_code_verification", 1, False, _sms_code, copy_code_button=True
    ),
}


# ─────────────────────────── Pure helpers ───────────────────────────

_PHONE_CHARS = re.compile(r"[^\d\s+().\- ]")


def normalize_tunisian_mobile(raw: Any) -> str | None:
    """+216XXXXXXXX for a Tunisian mobile (2x Ooredoo, 4x TT/MVNO, 5x Orange, 9x TT), else None.
    Landlines (3x, 7x) and special numbers (8x) can't receive WhatsApp/SMS reliably."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or _PHONE_CHARS.search(s):
        return None
    digits = re.sub(r"[\s().\- ]", "", s)
    if digits.startswith("+"):
        digits = digits[1:]
    elif digits.startswith("00"):
        digits = digits[2:]
    if not digits.isdigit():
        return None
    if len(digits) == 11 and digits.startswith("216"):
        digits = digits[3:]
    elif len(digits) != 8:
        return None
    if digits[0] not in "2459":
        return None
    return f"+216{digits}"


def _international_number(raw: Any, user: User | None, any_mobile: bool) -> str | None:
    """A foreign mobile in E.164 when it may receive our messages: the user's verified phone,
    or any foreign mobile for the verification code (`any_mobile`)."""
    if raw is None:
        return None
    try:
        number = to_e164(str(raw))
    except InvalidPhone:
        return None
    if not is_international_mobile(number):
        return None
    if any_mobile:
        return number
    if user is not None and user.phone_verified_at is not None and user.phone_e164 == number:
        return number
    return None


def sanitize_param(value: Any, max_len: int = 120) -> str:
    """Meta refuses template parameters with new lines, tabs or > 4 spaces in a row."""
    text = re.sub(r"[\r\n\t]+", " ", "" if value is None else str(value))
    text = re.sub(r" {2,}", " ", text).strip()[:max_len]
    return text or "-"


def build_template_payload(
    to: str, template_name: str, lang: str, params: list[str], copy_code: bool = False
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in params]})
    if copy_code and params and params[0]:
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": params[0]}],
            }
        )
    template: dict[str, Any] = {"name": template_name, "language": {"code": lang}}
    if components:
        template["components"] = components
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.removeprefix("+"),
        "type": "template",
        "template": template,
    }


TRANSIENT_CODES = {1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131049, 131056}
NO_WHATSAPP_CODES = {131026, 131047, 131050}  # no account, not reachable, opted out


def classify_whatsapp_error(http_status: int, code: int | None = None) -> ErrorClass:
    """What a Meta error means for us. http_status 0 = network error / timeout."""
    if code is not None:
        if code in TRANSIENT_CODES:
            return "transient"
        if code in NO_WHATSAPP_CODES:
            return "no_whatsapp"
        return "permanent"
    if http_status == 0 or http_status == 429 or http_status >= 500:
        return "transient"
    return "permanent"


STATUS_RANK = {"queued": 0, "retry_pending": 0, "sent": 1, "delivered": 2, "read": 3}


def next_status(current: str, incoming: str) -> str:
    """Webhooks arrive out of order: never go back from delivered/read."""
    if incoming == "failed":
        return current if STATUS_RANK.get(current, 0) >= 2 else "failed"
    if current == "failed":
        return incoming if incoming in ("delivered", "read") else current
    if incoming not in STATUS_RANK:
        return current
    return incoming if STATUS_RANK[incoming] > STATUS_RANK.get(current, 0) else current


_WINSMS_OK = {"ok", "success", "200", "0", "sent", "true"}
_WINSMS_ERROR_WORDS = re.compile(
    r"error|erreur|invalid|insuffisant|insufficient|failed|echec|échec|denied", re.I
)


def parse_winsms_response(http_status: int, text: str) -> dict[str, Any]:
    """WinSMS' reply format isn't documented publicly: be lenient."""
    if not 200 <= http_status < 300:
        return {"ok": False, "error": f"HTTP {http_status}: {text[:200]}"}
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = str(body.get("code", body.get("status", "")) or "").lower()
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        candidates = (
            body.get("ref"),
            body.get("message_id"),
            body.get("id"),
            data.get("ref"),
            data.get("id"),
        )
        ref = next((v for v in candidates if v is not None), None)
        if (code and code not in _WINSMS_OK) or body.get("error"):
            return {"ok": False, "error": str(body.get("message") or body.get("error") or code)[:300]}
        return {"ok": True, "ref": str(ref) if ref is not None else None}
    if _WINSMS_ERROR_WORDS.search(text):
        return {"ok": False, "error": text[:300]}
    return {"ok": True, "ref": None}


# Rows that did not reach a provider don't count against the limits.
NOT_SENT = ("disabled", "skipped_opt_out", "invalid_number", "rate_limited", "duplicate")


# ─────────────────────────── HTTP (patched by the tests) ───────────────────────────


def http_client() -> httpx.AsyncClient:
    """The client used for Meta and WinSMS; tests replace it with an httpx.MockTransport one."""
    return httpx.AsyncClient(timeout=settings.MESSAGING_HTTP_TIMEOUT_SECONDS)


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class WaResult:
    ok: bool
    attempts: int
    id: str | None = None
    error_class: ErrorClass | None = None
    code: str | None = None
    message: str | None = None


async def post_whatsapp(payload: dict[str, Any]) -> WaResult:
    """POST the template, retrying transient failures (3 tries: now, +0.5 s, +1.5 s)."""
    base = settings.WHATSAPP_API_BASE.rstrip("/")
    url = f"{base}/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    delays = (0.5, 1.5)
    last = WaResult(ok=False, attempts=0)
    async with http_client() as client:
        for attempt in range(1, 4):
            status, body = 0, None
            try:
                res = await client.post(url, json=payload, headers=headers)
                status = res.status_code
                try:
                    body = res.json()
                except ValueError:
                    body = None
            except httpx.HTTPError as exc:
                body = {"error": {"message": str(exc) or "network error"}}
            body = body if isinstance(body, dict) else {}
            messages = body.get("messages")
            msg_id = messages[0].get("id") if isinstance(messages, list) and messages else None
            if 200 <= status < 300 and msg_id:
                return WaResult(ok=True, attempts=attempt, id=str(msg_id))
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code: int | None = None
            if error.get("code") is not None:
                try:
                    code = int(error["code"])
                except (TypeError, ValueError):
                    code = None
            error_class = classify_whatsapp_error(status, code)
            details = (
                (error.get("error_data") or {}).get("details")
                if isinstance(error.get("error_data"), dict)
                else None
            )
            last = WaResult(
                ok=False,
                attempts=attempt,
                error_class=error_class,
                code=str(code) if code is not None else (f"http_{status}" if status else "network"),
                message=str(details or error.get("message") or f"HTTP {status}")[:300],
            )
            if error_class != "transient" or attempt == 3:
                return last
            await sleep(delays[attempt - 1])
    return last


async def post_sms(to: str, text: str) -> dict[str, Any]:
    params = {
        "action": "send-sms",
        "api_key": settings.WINSMS_API_KEY or "",
        "to": to.removeprefix("+"),
        "from": settings.WINSMS_SENDER or "",
        "sms": text,
        "response": "json",
    }
    last: dict[str, Any] = {"ok": False, "error": "not sent", "attempts": 0}
    async with http_client() as client:
        for attempt in (1, 2):
            try:
                res = await client.get(
                    settings.WINSMS_API_URL, params=params, headers={"Accept": "application/json"}
                )
                parsed = parse_winsms_response(res.status_code, res.text)
                last = {**parsed, "attempts": attempt}
                if parsed["ok"] or res.status_code < 500:
                    return last
            except httpx.HTTPError as exc:
                last = {"ok": False, "error": str(exc) or "network error", "attempts": attempt}
            if attempt < 2:
                await sleep(1.0)
    return last


# ─────────────────────────── Service ───────────────────────────


def _template_name(defn: TemplateDef) -> str:
    return getattr(settings, defn.setting) or defn.default_name


def _resolve_lang(wanted: Any, user_lang: Any) -> Lang:
    value = str(wanted or user_lang or settings.WHATSAPP_TEMPLATE_LANG or "fr").lower()
    return "ar" if value.startswith("ar") else "fr"


async def _insert(session: AsyncSession, values: dict[str, Any]) -> OutboundMessage | None:
    """Inserts a row claiming its idempotency key; None when the key is taken already."""
    row_id = (
        await session.execute(
            pg_insert(OutboundMessage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(OutboundMessage.id)
        )
    ).scalar_one_or_none()
    if row_id is None:
        return None
    return await session.get(OutboundMessage, row_id)


async def _by_key(session: AsyncSession, key: str) -> OutboundMessage | None:
    return (
        await session.execute(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    ).scalar_one_or_none()


async def _count_sent(session: AsyncSession, since: datetime, to: str | None = None) -> int:
    stmt = select(func.count()).where(
        OutboundMessage.created_at >= since, OutboundMessage.status.not_in(NOT_SENT)
    )
    if to is not None:
        stmt = stmt.where(OutboundMessage.to_e164 == to)
    return int((await session.execute(stmt)).scalar_one())


async def check_rate_limits(session: AsyncSession, to: str, now: datetime) -> str | None:
    """None when the message may go out, otherwise which limit it hits."""
    if await _count_sent(session, now - timedelta(minutes=10), to) >= settings.MSG_LIMIT_PER_NUMBER_10MIN:
        return "per_number"
    if await _count_sent(session, now - timedelta(days=1), to) >= settings.MSG_LIMIT_PER_NUMBER_DAY:
        return "per_number"
    if await _count_sent(session, now - timedelta(minutes=1)) >= settings.MSG_LIMIT_GLOBAL_MINUTE:
        return "global"
    if await _count_sent(session, now - timedelta(hours=1)) >= settings.MSG_LIMIT_GLOBAL_HOUR:
        return "global"
    return None


async def _child_sms(session: AsyncSession, parent: OutboundMessage) -> OutboundMessage | None:
    return (
        await session.execute(
            select(OutboundMessage)
            .where(OutboundMessage.parent_id == parent.id)
            .order_by(OutboundMessage.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def send_sms_for(session: AsyncSession, parent: OutboundMessage, reason: str) -> dict[str, Any]:
    """The SMS fallback of a WhatsApp row (once: its key is `<parent key>:sms`). WinSMS sends to
    Tunisian numbers only: a foreign number gets no SMS."""
    defn = TEMPLATES.get(parent.purpose)
    if defn is None or defn.sms is None or not (parent.to_e164 or "").startswith("+216"):
        parent.fallback_status = "skipped"
        await session.flush()
        return {"fallback": "skipped"}
    key = f"{parent.idempotency_key or parent.id}:sms"
    existing = await _by_key(session, key)
    if existing is not None:
        return {"fallback": "already", "sms_log_id": str(existing.id), "sms_status": existing.status}
    lang: Lang = "ar" if parent.lang == "ar" else "fr"
    text = defn.sms([sanitize_param(p) for p in parent.params or []], lang)
    base = {
        "channel": "sms", "purpose": parent.purpose, "lang": lang, "params": list(parent.params or []),
        "to_e164": parent.to_e164, "user_id": parent.user_id, "order_id": parent.order_id,
        "notification_id": parent.notification_id, "idempotency_key": key, "critical": parent.critical,
        "parent_id": parent.id,
    }  # fmt: skip
    if not settings.sms_enabled:
        row = await _insert(
            session,
            {**base, "status": "disabled", "provider": "none", "attempts": 0,
             "error_message": f"SMS off (no WinSMS secrets) — {reason}"},
        )  # fmt: skip
        if row is None:  # claimed concurrently
            return {"fallback": "already"}
        parent.fallback_status = "disabled"
        await session.flush()
        log.info("SMS fallback skipped (disabled) for %s: %s", parent.id, reason)
        return {"fallback": "disabled", "sms_log_id": str(row.id), "sms_status": "disabled"}
    row = await _insert(session, {**base, "status": "queued", "provider": "winsms", "attempts": 0})
    if row is None:
        return {"fallback": "already"}
    parent.fallback_status = "pending"
    await session.flush()
    res = await post_sms(parent.to_e164, text)
    now = now_utc()
    row.attempts = res["attempts"]
    if res["ok"]:
        row.status, row.provider_message_id, row.sent_at = "sent", res.get("ref") or "", now
    else:
        row.status, row.error_message, row.failed_at = "failed", (res.get("error") or "failed")[:300], now
    parent.fallback_status = "sent" if res["ok"] else "failed"
    await session.flush()
    status = "sent" if res["ok"] else "failed"
    return {"fallback": status, "sms_log_id": str(row.id), "sms_status": status}


async def send_template(
    session: AsyncSession,
    *,
    template_key: str,
    params: list[Any] | None,
    idempotency_key: str,
    to: str | None = None,
    user_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    notification_id: uuid.UUID | None = None,
    lang: str | None = None,
    critical: bool = False,
    international: bool = False,
) -> Result:
    """The Deno `send` action. `user_id` gives the phone, language and opt-in when `to` is absent.

    Tunisian mobiles only, except a foreign mobile that is the user's verified phone, or any
    foreign mobile when `international` (the verification code itself)."""
    defn = TEMPLATES.get(str(template_key or ""))
    if defn is None:
        return 400, {"error": "unknown template_key"}
    key = str(idempotency_key or "")[:200]
    if not key:
        return 400, {"error": "idempotency_key required"}
    clean = [sanitize_param(p) for p in (params if isinstance(params, list) else [])]
    if len(clean) != defn.params:
        return 400, {"error": f"template needs {defn.params} params"}

    prior = await _by_key(session, key)
    if prior is not None:
        return 200, {"success": True, "duplicate": True, "log_id": str(prior.id), "status": prior.status}

    user = await session.get(User, user_id) if user_id else None
    target = to or (user.phone_e164 if user else None)
    number = normalize_tunisian_mobile(target) or _international_number(target, user, international)
    language = _resolve_lang(lang, user.language if user else None)
    base = {
        "channel": "whatsapp", "purpose": template_key, "template_name": _template_name(defn),
        "lang": language, "params": clean,
        "to_e164": number or str(to or (user.phone_e164 if user else "") or "")[:40],
        "user_id": user.id if user else None, "order_id": order_id, "notification_id": notification_id,
        "idempotency_key": key, "critical": bool(critical), "attempts": 0, "fallback_status": "none",
    }  # fmt: skip

    async def refused(status: str, reason: str, error: str | None = None, **extra: Any) -> Result:
        row = await _insert(session, {**base, "status": status, "provider": "none", "error_message": error})
        if row is None:
            return await _duplicate(session, key)
        return 200, {"success": False, "reason": reason, **extra, "log_id": str(row.id)}

    if number is None:
        return await refused(
            "invalid_number", "invalid_number", "not a Tunisian mobile nor a verified foreign mobile"
        )
    if defn.requires_opt_in and (user is None or user.whatsapp_opt_in_at is None):
        return await refused("skipped_opt_out", "no_opt_in")
    limited = await check_rate_limits(session, number, now_utc())
    if limited:
        log.warning("WhatsApp rate limited (%s) %s…", limited, number[:7])
        return await refused("rate_limited", "rate_limited", limited, limit=limited)

    row = await _insert(
        session, {**base, "status": "queued", "provider": "meta" if settings.whatsapp_enabled else "none"}
    )
    if row is None:  # the same key claimed at the same moment
        return await _duplicate(session, key)

    if not settings.whatsapp_enabled:
        row.status, row.error_message = "disabled", "WhatsApp off (no Meta secrets)"
        await session.flush()
        log.info("WhatsApp disabled: %s for order %s not sent", template_key, order_id or "-")
        fb = await send_sms_for(session, row, "whatsapp_disabled") if critical else {}
        return 200, {"success": True, "whatsapp": "disabled", "log_id": str(row.id), **fb}

    res = await post_whatsapp(
        build_template_payload(number, row.template_name or "", language, clean, defn.copy_code_button)
    )
    at = now_utc()
    if res.ok:
        row.status, row.provider_message_id, row.attempts, row.sent_at = "sent", res.id, res.attempts, at
        if critical and defn.sms:
            row.fallback_deadline_at = at + timedelta(seconds=settings.WHATSAPP_FALLBACK_SECONDS or 60)
            row.fallback_status = "pending"
        await session.flush()
        return 200, {
            "success": True,
            "whatsapp": "sent",
            "log_id": str(row.id),
            "provider_message_id": res.id,
        }

    row.attempts, row.error_code, row.error_message = res.attempts, res.code, res.message
    # Transient failure after 3 tries: a critical alert can't wait -> SMS now; the others
    # are retried by check_pending.
    if res.error_class == "transient" and not critical:
        row.status, row.next_attempt_at = "retry_pending", at + timedelta(minutes=2)
        await session.flush()
        return 200, {"success": False, "whatsapp": "retry_pending", "log_id": str(row.id)}
    row.status, row.failed_at = "failed", at
    await session.flush()
    should_fallback = critical or res.error_class == "no_whatsapp"
    fb = await send_sms_for(session, row, f"whatsapp_{res.code}") if should_fallback else {}
    return 200, {"success": False, "whatsapp": "failed", "error_code": res.code, "log_id": str(row.id), **fb}


async def _duplicate(session: AsyncSession, key: str) -> Result:
    claimed = await _by_key(session, key)
    return 200, {"success": True, "duplicate": True, "log_id": str(claimed.id) if claimed else None}


async def fallback(session: AsyncSession, log_id: Any, reason: str | None = None) -> Result:
    row = await _get_row(session, log_id)
    if row is None or row.channel != "whatsapp":
        return 404, {"error": "log not found"}
    if row.status in ("delivered", "read"):
        return 200, {"success": True, "fallback": "not_needed"}
    return 200, {"success": True, **(await send_sms_for(session, row, reason or "webhook_failed"))}


async def _get_row(session: AsyncSession, log_id: Any) -> OutboundMessage | None:
    try:
        row_id = log_id if isinstance(log_id, uuid.UUID) else uuid.UUID(str(log_id))
    except ValueError:
        return None
    return await session.get(OutboundMessage, row_id, with_for_update=True)


async def check_pending(session: AsyncSession, order_id: uuid.UUID | None = None) -> Result:
    """SMS for critical WhatsApp rows past their deadline; retries of transient failures."""
    now = now_utc()
    stmt = select(OutboundMessage).where(OutboundMessage.channel == "whatsapp")
    stmt = (
        stmt.where(OutboundMessage.order_id == order_id)
        if order_id
        else stmt.where(OutboundMessage.fallback_status == "pending")
    )
    rows = list(
        (
            await session.execute(
                stmt.order_by(OutboundMessage.created_at.desc()).limit(50).with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    fallbacks = 0
    for row in rows:
        deadline = row.fallback_deadline_at
        if (
            row.fallback_status == "pending"
            and row.status == "sent"
            and deadline is not None
            and deadline <= now
        ):
            await send_sms_for(session, row, "not_delivered_in_time")
            fallbacks += 1
    if order_id:
        retry_rows = [r for r in rows if r.status == "retry_pending"]
    else:
        retry_rows = list(
            (
                await session.execute(
                    select(OutboundMessage)
                    .where(OutboundMessage.status == "retry_pending")
                    .order_by(OutboundMessage.created_at.desc())
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
    retried = 0
    for row in retry_rows:
        if (row.next_attempt_at is not None and row.next_attempt_at > now) or not settings.whatsapp_enabled:
            continue
        if row.attempts >= 9:
            row.status, row.failed_at = "failed", now
            continue
        defn = TEMPLATES.get(row.purpose)
        if defn is None:
            continue
        payload = build_template_payload(
            row.to_e164,
            row.template_name or "",
            row.lang or "fr",
            list(row.params or []),
            defn.copy_code_button,
        )
        res = await post_whatsapp(payload)
        retried += 1
        at = now_utc()
        row.attempts += res.attempts
        if res.ok:
            row.status, row.provider_message_id, row.sent_at = "sent", res.id, at
        elif res.error_class == "transient":
            row.next_attempt_at, row.error_code = at + timedelta(minutes=5), res.code
        else:
            row.status, row.error_code, row.error_message, row.failed_at = "failed", res.code, res.message, at
    await session.flush()
    return 200, {"success": True, "fallbacks": fallbacks, "retried": retried}


async def summary(session: AsyncSession, idempotency_key: Any) -> Result:
    row = await _by_key(session, str(idempotency_key or ""))
    if row is None:
        return 200, {"found": False}
    sms = await _child_sms(session, row)
    fallback_status = row.fallback_status or "none"
    return 200, {
        "found": True,
        "whatsapp": row.status,
        "whatsapp_error": row.error_code or None,
        "sms": sms.status if sms else (fallback_status if fallback_status != "none" else None),
        "fallback_status": fallback_status,
    }


# ─────────────────────────── Webhook statuses ───────────────────────────


@dataclass(frozen=True)
class StatusEvent:
    id: str
    status: str
    timestamp: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def extract_statuses(payload: Any) -> list[StatusEvent]:
    """entry[].changes[].value.statuses[] → flat list."""
    out: list[StatusEvent] = []

    def items(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    for entry in items(payload.get("entry") if isinstance(payload, dict) else None):
        for change in items(entry.get("changes") if isinstance(entry, dict) else None):
            value = change.get("value") if isinstance(change, dict) else None
            for s in items(value.get("statuses") if isinstance(value, dict) else None):
                if not isinstance(s, dict) or not s.get("id") or not s.get("status"):
                    continue
                errors = s.get("errors")
                err = (
                    errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else None
                )
                details = (
                    (err.get("error_data") or {}).get("details")
                    if err and isinstance(err.get("error_data"), dict)
                    else None
                )
                out.append(
                    StatusEvent(
                        id=str(s["id"]),
                        status=str(s["status"]),
                        timestamp=str(s["timestamp"]) if s.get("timestamp") else None,
                        error_code=str(err["code"]) if err and err.get("code") is not None else None,
                        error_message=str(details or err.get("message") or err.get("title") or "")[:300]
                        if err
                        else None,
                    )
                )
    return out


async def apply_statuses(session: AsyncSession, events: list[StatusEvent]) -> dict[str, int]:
    updated = fallbacks = 0
    for ev in events:
        row = (
            await session.execute(
                select(OutboundMessage)
                .where(OutboundMessage.provider_message_id == ev.id)
                .order_by(OutboundMessage.created_at)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        new = next_status(row.status, ev.status)
        if new == row.status:
            continue
        at = (
            datetime.fromtimestamp(int(ev.timestamp), UTC)
            if ev.timestamp and ev.timestamp.isdigit()
            else now_utc()
        )
        has_child = await _child_sms(session, row) is not None
        row.status = new
        if new == "delivered":
            row.delivered_at = at
        if new == "read":
            row.read_at = at
        if new in ("delivered", "read") and row.fallback_status == "pending" and not has_child:
            row.fallback_status = "none"
        if new == "failed":
            row.failed_at = at
            row.error_code = ev.error_code or row.error_code or "failed"
            row.error_message = ev.error_message or row.error_message or ""
        await session.flush()
        updated += 1
        # Failed after acceptance (e.g. 131026: no WhatsApp on this number): SMS for critical
        # alerts, and for any message whose recipient has no WhatsApp.
        if new == "failed" and not has_child and (row.critical or ev.error_code == "131026"):
            try:
                async with session.begin_nested():
                    await fallback(session, row.id, f"webhook_failed_{ev.error_code or ''}")
                fallbacks += 1
            except Exception:
                log.exception("webhook SMS fallback failed for %s", row.id)
    return {"updated": updated, "fallbacks": fallbacks}
