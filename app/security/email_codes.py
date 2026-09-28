"""Six-digit e-mail codes: hashed, 10 min TTL, 5 attempts, one open code per (user, purpose)."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import EmailCode
from app.security.tokens import now_utc

Purpose = Literal["verify", "reset", "migrate"]


def _hash(user_id: uuid.UUID, purpose: str, code: str) -> str:
    message = f"{user_id}:{purpose}:{code}".encode()
    return hmac.new(settings.JWT_SECRET.encode(), message, hashlib.sha256).hexdigest()


async def _open_code(session: AsyncSession, user_id: uuid.UUID, purpose: Purpose) -> EmailCode | None:
    return (
        await session.execute(
            select(EmailCode)
            .where(
                EmailCode.user_id == user_id,
                EmailCode.purpose == purpose,
                EmailCode.used_at.is_(None),
                EmailCode.expires_at > now_utc(),
            )
            .order_by(EmailCode.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class IssuedCode:
    code: str
    # Reset only: a long token for the e-mailed link (works without typing the e-mail address).
    link_token: str | None = None


async def issue_code(session: AsyncSession, user_id: uuid.UUID, purpose: Purpose) -> IssuedCode | None:
    """A fresh code (older open ones are voided), or None while the last one is too recent to resend."""
    current = await _open_code(session, user_id, purpose)
    if current is not None and now_utc() - current.created_at < timedelta(
        seconds=settings.EMAIL_CODE_RESEND_SECONDS
    ):
        return None
    await session.execute(
        update(EmailCode)
        .where(EmailCode.user_id == user_id, EmailCode.purpose == purpose, EmailCode.used_at.is_(None))
        .values(used_at=now_utc())
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    link_token = secrets.token_urlsafe(32) if purpose == "reset" else None
    session.add(
        EmailCode(
            user_id=user_id,
            purpose=purpose,
            code_hash=_hash(user_id, purpose, code),
            link_hash=hashlib.sha256(link_token.encode()).hexdigest() if link_token else None,
            expires_at=now_utc() + timedelta(minutes=settings.EMAIL_CODE_TTL_MINUTES),
        )
    )
    await session.flush()
    return IssuedCode(code=code, link_token=link_token)


async def consume_code(session: AsyncSession, user_id: uuid.UUID, purpose: Purpose, code: str) -> bool:
    """True once for the right code. Every wrong try counts; the code dies at the attempt limit.

    The caller must commit even on failure so the attempt is recorded.
    """
    row = await _open_code(session, user_id, purpose)
    if row is None:
        return False
    if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
        row.used_at = now_utc()
        return False
    candidate = code.strip()
    if (
        len(candidate) == 6
        and candidate.isdigit()
        and hmac.compare_digest(row.code_hash, _hash(user_id, purpose, candidate))
    ):
        row.used_at = now_utc()
        return True
    row.attempts += 1
    if row.attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
        row.used_at = now_utc()
    return False


async def consume_link_token(session: AsyncSession, token: str) -> uuid.UUID | None:
    """The user id of an open reset code whose link token this is (the code is consumed)."""
    row = (
        await session.execute(
            select(EmailCode)
            .where(
                EmailCode.link_hash == hashlib.sha256(token.strip().encode()).hexdigest(),
                EmailCode.purpose == "reset",
                EmailCode.used_at.is_(None),
                EmailCode.expires_at > now_utc(),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.used_at = now_utc()
    return row.user_id
