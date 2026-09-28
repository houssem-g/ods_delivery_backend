"""FastAPI dependencies: current_user, optional_user, require_admin."""

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.models import User
from app.security.tokens import InvalidToken, decode_access_token


@dataclass(frozen=True)
class CurrentUser:
    """The signed-in user as policies see it. Admin = users.role = 'admin'."""

    id: uuid.UUID
    email: str
    role: str
    full_name: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


async def load_active_user(session: AsyncSession, user_id: str | uuid.UUID) -> User | None:
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except ValueError:
        return None
    user = await session.get(User, uid)
    if user is None or user.deleted_at is not None or user.disabled_at is not None:
        return None
    return user


async def user_from_token(session: AsyncSession, token: str) -> CurrentUser | None:
    try:
        claims = decode_access_token(token)
    except InvalidToken:
        return None
    user = await load_active_user(session, claims["sub"])
    return to_current_user(user) if user else None


def to_current_user(user: User) -> CurrentUser:
    return CurrentUser(id=user.id, email=user.email, role=user.role, full_name=user.full_name)


async def optional_user(request: Request, session: AsyncSession = Depends(get_session)) -> CurrentUser | None:
    token = bearer_token(request)
    if token is None:
        return None
    user = await user_from_token(session, token)
    if user is None:
        raise ApiError(401, "unauthorized", "Invalid or expired token")
    return user


async def current_user(user: CurrentUser | None = Depends(optional_user)) -> CurrentUser:
    if user is None:
        raise ApiError(401, "auth_required", "Authentication required")
    return user


async def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    return user


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email.strip()))).scalar_one_or_none()
