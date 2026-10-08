"""« Se connecter avec l'empreinte / le visage » (phone app only).

After a normal sign-in the app enrols the phone: the server answers a random secret, the app
keeps it in the phone's secure storage behind the biometric check, and the server keeps only
its sha256. Signing in again = the phone checks the finger / face, releases the secret, the
server opens a normal session. A wrong secret for a known key revokes that key (copied or
guessed secret); a user has at most MAX_KEYS live keys (the oldest is dropped).
"""

import hmac
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import DeviceKey, User
from app.security.tokens import hash_token, now_utc

MAX_KEYS = 5


async def enroll(session: AsyncSession, user_id: uuid.UUID, label: str | None) -> tuple[DeviceKey, str]:
    live = (
        (
            await session.execute(
                select(DeviceKey)
                .where(DeviceKey.user_id == user_id, DeviceKey.revoked_at.is_(None))
                .order_by(DeviceKey.created_at)
            )
        )
        .scalars()
        .all()
    )
    for old in live[: max(0, len(live) - MAX_KEYS + 1)]:
        old.revoked_at = now_utc()
    secret = secrets.token_urlsafe(32)
    key = DeviceKey(user_id=user_id, secret_hash=hash_token(secret), label=(label or "").strip()[:80] or None)
    session.add(key)
    await session.flush()
    return key, secret


async def sign_in_with_key(session: AsyncSession, device_id: str, secret: str) -> User:
    refused = ApiError(
        401, "device_key_invalid", "Fingerprint / face sign-in is not available on this phone anymore"
    )
    try:
        key_id = uuid.UUID(device_id)
    except ValueError:
        raise refused from None
    key = await session.get(DeviceKey, key_id, with_for_update=True)
    if key is None or key.revoked_at is not None:
        raise refused
    if not hmac.compare_digest(key.secret_hash, hash_token(secret)):
        key.revoked_at = now_utc()
        await session.commit()
        raise refused
    user = await session.get(User, key.user_id)
    if user is None or user.deleted_at is not None or user.disabled_at is not None:
        raise refused
    key.last_used_at = now_utc()
    return user


async def revoke(session: AsyncSession, user_id: uuid.UUID, device_id: str) -> bool:
    try:
        key_id = uuid.UUID(device_id)
    except ValueError:
        return False
    key = await session.get(DeviceKey, key_id)
    if key is None or key.user_id != user_id or key.revoked_at is not None:
        return False
    key.revoked_at = now_utc()
    return True
