"""Account lifecycle: registration, password sign-in, e-mail codes, Google linking.

The router stays thin: it validates payloads, calls these functions, commits,
sets cookies and schedules the e-mails.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.config import settings
from app.errors import ApiError
from app.integrations.google_oauth import GoogleProfile
from app.models import User
from app.security.deps import find_user_by_email
from app.security.email_codes import Purpose, consume_code, consume_link_token, issue_code
from app.security.passwords import hash_password, password_problem, verify_password
from app.security.tokens import (
    IssuedRefresh,
    create_access_token,
    issue_refresh_token,
    now_utc,
    revoke_all_for_user,
)

# Longer than any typed code: only an e-mailed link token.
LINK_TOKEN_MIN_LENGTH = 20


@dataclass(frozen=True)
class PendingEmail:
    """A code to e-mail once the transaction is committed."""

    to: str
    purpose: Purpose
    code: str
    language: str
    link_token: str | None = None


class AuthRefusal(ApiError):
    """A refusal that may still owe the user an e-mail (sent after the commit)."""

    def __init__(self, status: int, error: str, message: str, pending: PendingEmail | None = None) -> None:
        super().__init__(status, error, message)
        self.pending = pending


@dataclass(frozen=True)
class SignedIn:
    body: dict[str, Any]
    refresh: IssuedRefresh


def serialize_me(user: User) -> dict[str, Any]:
    """Legacy User object (`auth.me()`): role is 'admin' or 'user'."""
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": "admin" if user.role == "admin" else "user",
        "is_verified": user.email_verified_at is not None,
        "disabled": user.disabled_at is not None,
        "language": user.language,
        "has_password": user.password_hash is not None,
        "google_linked": user.google_sub is not None,
        # « Vous vous faites livrer combien de fois par mois ? » (customer profile), null = not answered
        "declared_monthly_orders": user.declared_monthly_orders,
        "created_date": legacy_datetime(user.created_at),
        "updated_date": legacy_datetime(user.updated_at),
    }


def check_new_password(password: str) -> None:
    problem = password_problem(password, settings.PASSWORD_MIN_LENGTH)
    if problem:
        raise ApiError(400, "weak_password", problem)


async def sign_in(session: AsyncSession, user: User, family: uuid.UUID | None = None) -> SignedIn:
    access, expires = create_access_token(user.id, user.role)
    refresh = await issue_refresh_token(session, user.id, family=family)
    body = {
        "access_token": access,
        "token_type": "bearer",
        "expires_at": legacy_datetime(expires),
        "user": serialize_me(user),
    }
    return SignedIn(body=body, refresh=refresh)


async def _code_for(session: AsyncSession, user: User, purpose: Purpose) -> PendingEmail | None:
    issued = await issue_code(session, user.id, purpose)
    if issued is None:
        return None
    return PendingEmail(user.email, purpose, issued.code, user.language, issued.link_token)


async def register(session: AsyncSession, email: str, password: str, full_name: str) -> PendingEmail | None:
    check_new_password(password)
    user = await find_user_by_email(session, email)
    if user is not None and user.deleted_at is not None:
        raise ApiError(409, "email_taken", "User already exists")
    if user is not None and user.password_hash is None:
        # Migrated or Google-only account: the owner proves the address with a code first.
        pending = await _code_for(session, user, "migrate")
        raise AuthRefusal(409, "account_setup_required", "Account setup required", pending)
    if user is not None and user.email_verified_at is not None:
        raise ApiError(409, "email_taken", "User already exists")
    if user is None:
        user = User(email=email.strip(), full_name=full_name.strip())
        session.add(user)
    else:
        # Never verified: whoever proves the address owns it, so the latest sign-up wins.
        user.full_name = full_name.strip()
    user.password_hash = hash_password(password)
    await session.flush()
    return await _code_for(session, user, "verify")


async def login(session: AsyncSession, email: str, password: str) -> User:
    """The user to sign in, or an AuthRefusal."""
    user = await find_user_by_email(session, email)
    if user is None or user.deleted_at is not None:
        verify_password(password, None)
        raise ApiError(401, "invalid_credentials", "Invalid email or password")
    if user.password_hash is None:
        pending = await _code_for(session, user, "migrate")
        raise AuthRefusal(
            409, "account_setup_required", "Account setup required: a code was sent to your e-mail", pending
        )
    if not verify_password(password, user.password_hash):
        raise ApiError(401, "invalid_credentials", "Invalid email or password")
    if user.disabled_at is not None:
        raise ApiError(403, "account_disabled", "This account is disabled")
    if user.email_verified_at is None:
        raise ApiError(403, "email_not_verified", "Email not verified: please confirm your email")
    return user


async def verify_email(session: AsyncSession, email: str, code: str) -> User:
    user = await find_user_by_email(session, email)
    if (
        user is None
        or user.deleted_at is not None
        or not await consume_code(session, user.id, "verify", code)
    ):
        raise ApiError(400, "invalid_code", "Invalid or expired code")
    user.email_verified_at = user.email_verified_at or now_utc()
    return user


async def resend_code(session: AsyncSession, email: str) -> PendingEmail | None:
    """Anti-enumeration: the caller always answers the same thing."""
    user = await find_user_by_email(session, email)
    if user is None or user.deleted_at is not None:
        return None
    if user.password_hash is None:
        return await _code_for(session, user, "migrate")
    if user.email_verified_at is None:
        return await _code_for(session, user, "verify")
    return None


async def request_reset(session: AsyncSession, email: str) -> PendingEmail | None:
    user = await find_user_by_email(session, email)
    if user is None or user.deleted_at is not None or user.disabled_at is not None:
        return None
    return await _code_for(session, user, "reset")


async def set_password_with_code(
    session: AsyncSession, email: str | None, code: str, new_password: str, purpose: Purpose
) -> User:
    """reset-password and account-setup: a valid code proves the address, so it also verifies it.

    A reset may also come from the e-mailed link: then `code` is the link token and no
    e-mail address is needed.
    """
    check_new_password(new_password)
    user = await _user_for_code(session, email, code, purpose)
    if user is None:
        raise ApiError(400, "invalid_code", "Invalid or expired code")
    user.password_hash = hash_password(new_password)
    user.email_verified_at = user.email_verified_at or now_utc()
    await revoke_all_for_user(session, user.id)
    return user


async def _user_for_code(
    session: AsyncSession, email: str | None, code: str, purpose: Purpose
) -> User | None:
    if email:
        user = await find_user_by_email(session, email)
        if user is None or user.deleted_at is not None:
            return None
        return user if await consume_code(session, user.id, purpose, code) else None
    if purpose != "reset" or len(code) < LINK_TOKEN_MIN_LENGTH:
        return None
    user_id = await consume_link_token(session, code)
    user = await session.get(User, user_id) if user_id else None
    return user if user is not None and user.deleted_at is None else None


async def change_password(session: AsyncSession, user: User, current: str, new: str) -> None:
    if user.password_hash is not None and not verify_password(current, user.password_hash):
        raise ApiError(400, "invalid_credentials", "Current password is incorrect")
    check_new_password(new)
    user.password_hash = hash_password(new)
    await revoke_all_for_user(session, user.id)


async def link_google(session: AsyncSession, profile: GoogleProfile) -> User:
    """Sign in with Google: by google_sub, else link to the account with the same verified e-mail."""
    if not profile.email_verified:
        raise ApiError(403, "google_email_unverified", "Google did not verify this e-mail")
    user = (await session.execute(select(User).where(User.google_sub == profile.sub))).scalar_one_or_none()
    if user is None:
        user = await find_user_by_email(session, profile.email)
        if user is None:
            user = User(email=profile.email, full_name=profile.name)
            session.add(user)
        user.google_sub = profile.sub
    if user.deleted_at is not None or user.disabled_at is not None:
        raise ApiError(403, "account_disabled", "This account is disabled")
    user.email_verified_at = user.email_verified_at or now_utc()
    if not user.full_name and profile.name:
        user.full_name = profile.name
    await session.flush()
    return user
