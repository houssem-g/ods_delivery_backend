"""Test data helpers (direct database writes; the API is exercised by the tests themselves)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.db import SessionLocal
from app.models import Courier, User, UserAddress
from app.security.passwords import hash_password
from app.security.tokens import create_access_token

PASSWORD = "Correct-Horse-9"
PASSWORD_HASH = hash_password(PASSWORD)


class Factory:
    async def user(
        self,
        email: str | None = None,
        password: str | None = PASSWORD,
        verified: bool = True,
        role: str = "customer",
        profile: bool = True,
        **fields: Any,
    ) -> User:
        async with SessionLocal() as s:
            user = User(
                email=email or f"user-{uuid.uuid4().hex[:8]}@example.test",
                password_hash=(PASSWORD_HASH if password == PASSWORD else hash_password(password))
                if password
                else None,
                email_verified_at=datetime.now(UTC) if verified else None,
                full_name=fields.pop("full_name", "Test User"),
                role=role,
                profile_created_at=datetime.now(UTC) if profile else None,
                **fields,
            )
            s.add(user)
            await s.commit()
            await s.refresh(user)
            return user

    async def address(self, user: User, **fields: Any) -> UserAddress:
        async with SessionLocal() as s:
            row = UserAddress(user_id=user.id, is_default=True, **fields)
            s.add(row)
            await s.commit()
            return row

    async def courier(self, user: User, **fields: Any) -> Courier:
        async with SessionLocal() as s:
            row = Courier(
                user_id=user.id,
                display_name=fields.pop("display_name", "Courier"),
                phone_e164=fields.pop("phone_e164", "+21622123456"),
                id_document_number=fields.pop("id_document_number", "12345678"),
                vehicle=fields.pop("vehicle", "scooter"),
                price_per_km=fields.pop("price_per_km", Decimal("1.000")),
                **fields,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row


def token_for(user: User) -> str:
    return create_access_token(user.id, user.role)[0]


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user)}"}


def error_of(response: httpx.Response) -> str:
    return response.json().get("error")
