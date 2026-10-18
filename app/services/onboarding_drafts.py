"""Onboarding drafts: the sign-up a new account has not finished, kept server side (owner
18/10/2026: « quand quelqu'un part chercher le code OTP ou upload une image, tout recommence »).

One draft per (user, kind): `courier` = CourierOnboarding (step, answers, the ID photo's
private file URI as soon as it is uploaded), `customer` = the CustomerProfile setup (address,
phone...). The payload is the front's form state, opaque here (a JSON object, 16 KB at most),
except that anything named like a password, token or secret is dropped: those never reach the
database. Kept 30 days after its last save; expired rows are hidden at once and purged hourly
(job `purge_order_drafts`). Creating the courier profile deletes the courier draft.
"""

import json
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import OnboardingDraft
from app.security.tokens import now_utc

TTL = timedelta(days=30)
MAX_PAYLOAD_BYTES = 16 * 1024
KINDS = frozenset({"courier", "customer"})
_SECRET_KEY = re.compile(r"password|passwd|secret|token|otp", re.IGNORECASE)


class DraftRefused(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error}


def _kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise DraftRefused(400, "invalid_kind")
    return kind


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _without_secrets(v)
            for k, v in value.items()
            if not (isinstance(k, str) and _SECRET_KEY.search(k))
        }
    if isinstance(value, list):
        return [_without_secrets(v) for v in value]
    return value


def as_dict(draft: OnboardingDraft) -> dict[str, Any]:
    return {
        "kind": draft.kind,
        "payload": draft.payload,
        "created_date": legacy_datetime(draft.created_at),
        "updated_date": legacy_datetime(draft.updated_at),
        "expires_at": legacy_datetime(draft.expires_at),
    }


async def get_live(session: AsyncSession, user_id: uuid.UUID, kind: Any) -> OnboardingDraft | None:
    clean = _kind(kind)
    return (
        await session.execute(
            select(OnboardingDraft).where(
                OnboardingDraft.user_id == user_id,
                OnboardingDraft.kind == clean,
                OnboardingDraft.expires_at > now_utc(),
            )
        )
    ).scalar_one_or_none()


async def save(session: AsyncSession, user_id: uuid.UUID, kind: Any, payload: Any) -> OnboardingDraft:
    """Upsert the caller's draft of this kind (the 30 days restart)."""
    clean_kind = _kind(kind)
    if not isinstance(payload, dict):
        raise DraftRefused(400, "invalid_payload")
    clean = _without_secrets(payload)
    if len(json.dumps(clean, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
        raise DraftRefused(413, "payload_too_large")
    now = now_utc()
    statement = insert(OnboardingDraft).values(
        user_id=user_id, kind=clean_kind, payload=clean, created_at=now, updated_at=now, expires_at=now + TTL
    )
    statement = statement.on_conflict_do_update(
        index_elements=[OnboardingDraft.user_id, OnboardingDraft.kind],
        set_={"payload": clean, "updated_at": now, "expires_at": now + TTL},
    ).returning(OnboardingDraft)
    draft = (await session.execute(statement, execution_options={"populate_existing": True})).scalar_one()
    return draft


async def remove(session: AsyncSession, user_id: uuid.UUID, kind: Any) -> bool:
    clean = _kind(kind)
    result = await session.execute(
        delete(OnboardingDraft).where(OnboardingDraft.user_id == user_id, OnboardingDraft.kind == clean)
    )
    return (result.rowcount or 0) > 0


async def remove_all(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(OnboardingDraft).where(OnboardingDraft.user_id == user_id))


async def purge_expired(session: AsyncSession) -> int:
    result = await session.execute(delete(OnboardingDraft).where(OnboardingDraft.expires_at <= now_utc()))
    return int(result.rowcount or 0)
