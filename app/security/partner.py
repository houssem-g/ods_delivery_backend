"""Partner authentication: `X-Partner-Key` → active partner → its technical Delivery user.

A partner (an ODS restaurant, app/models/partners.py) calls /api/partner/v1 with the key an admin
created for it (shown once; only its sha256 is stored). The requests then run as the partner's
technical user — a plain customer account — so the order services keep their rules.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import ApiError
from app.models import Partner
from app.rate_limit import allow
from app.security.deps import CurrentUser, load_active_user, to_current_user
from app.security.tokens import now_utc

HEADER = "x-partner-key"
KEY_PREFIX = "odsd_live_"
PREFIX_SHOWN = len(KEY_PREFIX) + 4  # "odsd_live_Ab3x": enough to tell keys apart, useless to guess
LAST_USED_EVERY_SECONDS = 60


def generate_key() -> str:
    """A new partner key: the prefix + 43 url-safe characters (256 bits)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def generate_secret() -> str:
    """A webhook signing secret (shown once, kept as is: the server signs with it)."""
    return secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def key_prefix(key: str) -> str:
    return key[:PREFIX_SHOWN]


@dataclass(frozen=True)
class PartnerContext:
    partner: Partner
    user: CurrentUser  # the technical user the partner's orders belong to

    @property
    def id(self) -> uuid.UUID:
        return self.partner.id


async def partner_by_key(session: AsyncSession, key: str) -> Partner | None:
    return (
        await session.execute(select(Partner).where(Partner.key_hash == hash_key(key)))
    ).scalar_one_or_none()


async def current_partner(request: Request, session: AsyncSession = Depends(get_session)) -> PartnerContext:
    key = (request.headers.get(HEADER) or "").strip()
    if not key:
        raise ApiError(401, "partner_key_required", "X-Partner-Key header required")
    partner = await partner_by_key(session, key)
    if partner is None or not partner.active or partner.revoked_at is not None:
        raise ApiError(401, "partner_key_invalid", "Unknown or revoked partner key")
    if not allow("partner", partner.key_hash, settings.RATE_LIMIT_PARTNER):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    user = await load_active_user(session, partner.technical_user_id)
    if user is None:
        raise ApiError(401, "partner_key_invalid", "Partner account disabled")
    now = now_utc()
    if (
        partner.last_used_at is None
        or (now - partner.last_used_at).total_seconds() >= LAST_USED_EVERY_SECONDS
    ):
        partner.last_used_at = now
        await session.flush()
    return PartnerContext(partner=partner, user=to_current_user(user))
