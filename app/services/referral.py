"""Courier invite links: resolveReferralCode (public card) and getCourierReferralStats.

Codes: 5-8 characters of the alphabet without 0/O, 1/I/L (src/lib/referral.js), stored
upper case in `couriers.referral_code` (unique). A referred customer counts when the
attribution was written at sign-up: `users.referred_at` within 24 h of the profile's
creation (not added later by editing the profile), the courier's own account excluded,
deleted accounts excluded. Only numbers leave getCourierReferralStats, never a
customer's name, e-mail or phone.
"""

import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Courier, Order, User, courier_stats
from app.realtime.events import emit
from app.security.deps import CurrentUser

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{5,8}$")
SIGNUP_WINDOW = timedelta(hours=24)
CODE_ATTEMPTS = 8


def normalize_code(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    return code if CODE_RE.match(code) else None


def first_name(full_name: str | None) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else ""


def random_code(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


async def resolve_code(session: AsyncSession, raw_code: Any, caller: CurrentUser | None) -> dict[str, Any]:
    """resolveReferralCode answer (always 200): the public card or {valid: false, reason}."""
    code = normalize_code(raw_code)
    if code is None:
        return {"valid": False, "reason": "invalid_code"}
    row = (
        await session.execute(
            select(Courier, courier_stats.c.total_deliveries, courier_stats.c.average_rating)
            .join(User, User.id == Courier.user_id)
            .outerjoin(courier_stats, courier_stats.c.courier_id == Courier.id)
            .where(Courier.referral_code == code, User.deleted_at.is_(None))
        )
    ).first()
    if row is None or row.Courier.verification == "rejected":
        return {"valid": False, "reason": "unknown_code"}
    courier: Courier = row.Courier
    if caller is not None and courier.user_id == caller.id:
        return {"valid": False, "reason": "self", "is_self": True}
    deliveries = int(row.total_deliveries or 0)
    rating = float(row.average_rating) if deliveries > 0 and row.average_rating else None
    return {
        "valid": True,
        "code": code,
        "courier_id": str(courier.id),
        "first_name": first_name(courier.display_name),
        # never rated: no rating shown (not a default 5)
        "rating": rating,
        "total_deliveries": deliveries,
        "vehicle_type": courier.vehicle or None,
    }


async def ensure_code(session: AsyncSession, courier: Courier) -> str:
    """The courier's code, generated (5 chars, 6 after 5 clashes) the first time."""
    current = (courier.referral_code or "").strip().upper()
    if CODE_RE.match(current):
        if current != courier.referral_code:
            courier.referral_code = current
            await session.flush()
        return current
    for attempt in range(CODE_ATTEMPTS):
        code = random_code(5 if attempt < 5 else 6)
        taken = (await session.execute(select(Courier.id).where(Courier.referral_code == code))).first()
        if taken is not None:
            continue
        try:
            async with session.begin_nested():
                courier.referral_code = code
                await session.flush()
        except IntegrityError:  # taken concurrently
            continue
        emit(session, "CourierProfile", "update", courier.id)
        return code
    raise ApiError(500, "referral_code_unavailable", "Could not allocate a referral code")


def signup_attribution(courier: Courier) -> Any:
    """SQL: customers attributed to this courier at sign-up (isSignupAttribution)."""
    return and_(
        User.referred_by_courier_id == courier.id,
        User.id != courier.user_id,
        User.deleted_at.is_(None),
        User.profile_created_at.is_not(None),
        User.referred_at.is_not(None),
        func.abs(func.extract("epoch", User.referred_at - User.profile_created_at))
        <= SIGNUP_WINDOW.total_seconds(),
    )


async def courier_stats_for(session: AsyncSession, user: CurrentUser) -> dict[str, Any]:
    """getCourierReferralStats: {code, verified, referred_count, ordered_count,
    delivered_orders, delivered_by_me}; 404 no_courier_profile."""
    courier = (
        await session.execute(select(Courier).where(Courier.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if courier is None:
        raise ApiError(404, "no_courier_profile", "No courier profile")
    code = await ensure_code(session, courier)
    referred = select(User.id).where(signup_attribution(courier))
    count = select(func.count()).select_from(referred.subquery())
    referred_count = (await session.execute(count)).scalar_one()
    delivered = (
        await session.execute(
            select(
                func.count(),
                func.count(func.distinct(Order.customer_id)),
                func.count().filter(Order.courier_id == courier.id),
            ).where(
                Order.preferred_courier_id == courier.id,
                Order.status == "delivered",
                Order.customer_id.in_(referred),
            )
        )
    ).one()
    return {
        "code": code,
        "verified": courier.verification == "verified",
        "referred_count": referred_count,
        "ordered_count": delivered[1],
        "delivered_orders": delivered[0],
        "delivered_by_me": delivered[2],
    }
