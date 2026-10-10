"""Access JWTs and rotating refresh tokens (ods-be pattern: sha256 in DB, family revocation on reuse)."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Response
from jwt import PyJWTError as JWTError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DeviceKey, RefreshToken

REFRESH_GRACE_SECONDS = 30


class InvalidToken(Exception):
    pass


class RefreshReused(InvalidToken):
    """A revoked refresh token was presented again: its whole family is revoked."""


def now_utc() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, datetime]:
    issued = now_utc()
    expires = issued + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES)
    claims = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), expires


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if claims.get("type") != "access" or not claims.get("sub"):
        raise InvalidToken("not an access token")
    return claims


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedRefresh:
    raw: str
    user_id: uuid.UUID
    family: uuid.UUID
    expires_at: datetime


async def issue_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, family: uuid.UUID | None = None
) -> IssuedRefresh:
    raw = secrets.token_urlsafe(48)
    expires = now_utc() + timedelta(days=settings.REFRESH_TOKEN_DAYS)
    fam = family or uuid.uuid4()
    session.add(RefreshToken(user_id=user_id, token_hash=hash_token(raw), family=fam, expires_at=expires))
    await session.flush()
    return IssuedRefresh(raw=raw, user_id=user_id, family=fam, expires_at=expires)


async def revoke_family(session: AsyncSession, family: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family == family, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Every session and every fingerprint / face sign-in of the user (new password, disabled
    or deleted account)."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )
    await session.execute(
        update(DeviceKey)
        .where(DeviceKey.user_id == user_id, DeviceKey.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )


async def consume_refresh_token(session: AsyncSession, raw: str) -> RefreshToken:
    """Rotate: mark the presented token replaced; the caller issues its successor in the family.

    Two tabs share the cookie and may refresh at once: a token rotated less than
    REFRESH_GRACE_SECONDS ago, in a family that is still alive, is accepted again
    (the second tab gets its own successor). Any other reuse means the token leaked
    or was replayed: the whole family is revoked and the owner signs in again.
    The caller commits.
    """
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidToken("unknown refresh token")
    now = now_utc()
    if row.revoked_at is not None:
        in_grace = row.rotated_at is not None and now - row.rotated_at <= timedelta(
            seconds=REFRESH_GRACE_SECONDS
        )
        if in_grace and await _family_alive(session, row.family):
            return row
        await revoke_family(session, row.family)
        raise RefreshReused("refresh token reused")
    if row.expires_at <= now:
        raise InvalidToken("refresh token expired")
    row.revoked_at = row.rotated_at = now
    return row


async def _family_alive(session: AsyncSession, family: uuid.UUID) -> bool:
    live = await session.scalar(
        select(RefreshToken.id)
        .where(
            RefreshToken.family == family,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now_utc(),
        )
        .limit(1)
    )
    return live is not None


async def revoke_refresh_token(session: AsyncSession, raw: str) -> uuid.UUID | None:
    """Logout: the presented token's family ends (other devices keep their sessions). Returns its
    user (None for an unknown token)."""
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if row is None:
        return None
    await revoke_family(session, row.family)
    return row.user_id


def set_refresh_cookie(response: Response, issued: IssuedRefresh) -> None:
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=issued.raw,
        max_age=settings.REFRESH_TOKEN_DAYS * 86400,
        path=settings.REFRESH_COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN or None,
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response) -> None:
    # Attributes must match set_refresh_cookie or the browser keeps the cookie.
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path=settings.REFRESH_COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN or None,
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
