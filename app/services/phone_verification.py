"""Phone verification by WhatsApp for foreign customer numbers (owner's decision, 2026-09-29).

Tunisian numbers (+216) are accepted as they are. A foreign number must be confirmed with a
6-digit code sent by WhatsApp (template `verification_code`) before the customer can order.
Codes follow the e-mail codes (app/security/email_codes.py): HMAC-hashed, 10 min, 5 attempts,
constant-time compare, one open code per user. The `phone_verifications` rows are also the
rate-limit ledger: 3 codes / 10 min / user, 5 codes / day / number (rows are kept even when
the send failed: the function commits its refusals).
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import OutboundMessage, PhoneVerification, User
from app.realtime.events import emit
from app.security.tokens import now_utc
from app.services import whatsapp
from app.services.phones import InvalidPhone, is_international_mobile, is_tunisian, to_e164

log = logging.getLogger("odsd.phone_verification")

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
USER_LIMIT_10MIN = 3
NUMBER_LIMIT_DAY = 5

UNAVAILABLE_MESSAGE = "WhatsApp verification is not available yet. Use a Tunisian phone number to order."


class VerificationRefused(Exception):
    def __init__(self, status: int, error: str, message: str | None = None, **extra: Any) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.error, **self.extra}
        if self.message:
            body["message"] = self.message
        return body


def _hash(user_id: uuid.UUID, phone: str, code: str) -> str:
    message = f"phone:{user_id}:{phone}:{code}".encode()
    return hmac.new(settings.JWT_SECRET.encode(), message, hashlib.sha256).hexdigest()


def normalize(raw: Any) -> str:
    """E.164 or VerificationRefused(400)."""
    if not isinstance(raw, str) or not raw.strip():
        raise VerificationRefused(400, "phone_required")
    try:
        phone = to_e164(raw)
    except InvalidPhone as exc:
        raise VerificationRefused(400, "invalid_phone") from exc
    if phone is None:
        raise VerificationRefused(400, "phone_required")
    return phone


async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    return (await session.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()


async def _count(session: AsyncSession, *where: Any) -> int:
    stmt = select(func.count()).select_from(PhoneVerification).where(*where)
    return int((await session.execute(stmt)).scalar_one())


async def request_code(session: AsyncSession, user_id: uuid.UUID, raw_phone: Any) -> dict[str, Any]:
    """requestPhoneVerification. Raises VerificationRefused."""
    phone = normalize(raw_phone)
    if is_tunisian(phone):
        return {"verified": True, "required": False}
    user = await _lock_user(session, user_id)
    if user.phone_e164 == phone and user.phone_verified_at is not None:
        return {"verified": True, "required": True}
    if not is_international_mobile(phone):
        raise VerificationRefused(400, "invalid_phone", "This number can't receive WhatsApp messages.")
    if not settings.whatsapp_enabled:
        raise VerificationRefused(503, "whatsapp_unavailable", UNAVAILABLE_MESSAGE)

    now = now_utc()
    per_user = await _count(
        session,
        PhoneVerification.user_id == user.id,
        PhoneVerification.created_at > now - timedelta(minutes=10),
    )
    per_number = await _count(
        session,
        PhoneVerification.phone_e164 == phone,
        PhoneVerification.created_at > now - timedelta(days=1),
    )
    if per_user >= USER_LIMIT_10MIN or per_number >= NUMBER_LIMIT_DAY:
        raise VerificationRefused(
            429,
            "too_many_codes",
            "Too many codes requested. Try again later.",
            retry_after_seconds=600 if per_user >= USER_LIMIT_10MIN else 86400,
        )

    await session.execute(
        update(PhoneVerification)
        .where(PhoneVerification.user_id == user.id, PhoneVerification.used_at.is_(None))
        .values(used_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = PhoneVerification(
        user_id=user.id, phone_e164=phone, code_hash=_hash(user.id, phone, code), expires_at=now + CODE_TTL
    )
    session.add(row)
    await session.flush()
    _status, answer = await whatsapp.send_template(
        session,
        template_key="verification_code",
        params=[code],
        idempotency_key=f"phoneverify:{row.id}",
        to=phone,
        user_id=user.id,
        international=True,
    )
    if answer.get("whatsapp") == "sent":
        return {"sent": True, "channel": "whatsapp", "expires_in": int(CODE_TTL.total_seconds())}

    # Not sent: the code is useless, and a late retry (check_pending) must not deliver it.
    row.used_at = now
    if answer.get("whatsapp") == "retry_pending" and answer.get("log_id"):
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == uuid.UUID(answer["log_id"]))
            .values(status="failed", failed_at=now, next_attempt_at=None)
        )
    await session.flush()
    reason = answer.get("reason") or answer.get("error_code") or answer.get("whatsapp")
    log.info("phone verification code not sent: %s", reason)
    if answer.get("reason") == "rate_limited":
        raise VerificationRefused(
            429, "too_many_codes", "Too many codes requested. Try again later.", retry_after_seconds=600
        )
    if answer.get("error_code") in {str(c) for c in whatsapp.NO_WHATSAPP_CODES}:
        raise VerificationRefused(400, "no_whatsapp", "This number has no WhatsApp account.")
    raise VerificationRefused(502, "whatsapp_failed", "The WhatsApp message could not be sent. Try again.")


async def confirm_code(
    session: AsyncSession, user_id: uuid.UUID, raw_phone: Any, raw_code: Any
) -> dict[str, Any]:
    """confirmPhoneVerification. Raises VerificationRefused; the caller commits either way (the
    attempt counts)."""
    phone = normalize(raw_phone)
    if is_tunisian(phone):
        return {"verified": True, "required": False}
    user = await _lock_user(session, user_id)
    row = (
        await session.execute(
            select(PhoneVerification)
            .where(PhoneVerification.user_id == user.id, PhoneVerification.phone_e164 == phone)
            .order_by(PhoneVerification.created_at.desc(), PhoneVerification.id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = now_utc()
    if row is None:
        raise VerificationRefused(400, "invalid_code")
    if row.attempts >= MAX_ATTEMPTS:
        raise VerificationRefused(400, "too_many_attempts")
    if row.used_at is not None:
        if user.phone_e164 == phone and user.phone_verified_at is not None:
            return {"verified": True, "phone": phone}  # the same code sent twice
        raise VerificationRefused(400, "code_expired")
    if row.expires_at <= now:
        row.used_at = now
        raise VerificationRefused(400, "code_expired")

    code = str(raw_code or "").strip()
    if len(code) == 6 and code.isdigit() and hmac.compare_digest(row.code_hash, _hash(user.id, phone, code)):
        row.used_at = now
        user.phone_e164 = phone
        user.phone_verified_at = now
        await session.flush()
        emit(session, "UserProfile", "update", user.id)
        return {"verified": True, "phone": phone}

    row.attempts += 1
    if row.attempts >= MAX_ATTEMPTS:
        row.used_at = now
        await session.flush()
        raise VerificationRefused(400, "too_many_attempts")
    await session.flush()
    raise VerificationRefused(400, "invalid_code", attempts_left=MAX_ATTEMPTS - row.attempts)
