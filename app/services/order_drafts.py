"""Order drafts: the unfinished NewOrder form, kept 24 h after its last save (owner's request,
2026-09-29: "Les commandes non finies restent comme brouillon pendant 24h et je dois pouvoir
les supprimer").

The payload is the front's form state, opaque here (a JSON object, 20 KB at most). The title is
a short summary for the lists: the first item name or the shop name (or the `title` the front
sends). At most MAX_DRAFTS live drafts per user: saving one more drops the oldest. Expired rows
are hidden at once and purged hourly (job `purge_order_drafts`).
"""

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import OrderDraft
from app.security.tokens import now_utc

TTL = timedelta(hours=24)
MAX_DRAFTS = 10
MAX_PAYLOAD_BYTES = 20 * 1024
TITLE_MAX = 120


class DraftRefused(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error}


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str | int | float) else ""


def derive_title(payload: dict[str, Any], given: Any = None) -> str:
    """The given title, else the first item name, the items text's first line, the shop name."""
    candidates: list[Any] = [given]
    items = payload.get("items")
    if isinstance(items, list):
        candidates += [i.get("name") for i in items if isinstance(i, dict)]
    items_text = payload.get("items_text")
    if isinstance(items_text, str):
        candidates += items_text.splitlines()[:1]
    shop = payload.get("shopInfo")
    if isinstance(shop, dict):
        candidates.append(shop.get("shop_name"))
    candidates.append(payload.get("shop_name"))
    for candidate in candidates:
        text = _clean(candidate) if candidate is not None else ""
        if text:
            return text[:TITLE_MAX]
    return ""


def as_dict(draft: OrderDraft) -> dict[str, Any]:
    return {
        "id": str(draft.id),
        "title": draft.title,
        "payload": draft.payload,
        "created_date": legacy_datetime(draft.created_at),
        "updated_date": legacy_datetime(draft.updated_at),
        "expires_at": legacy_datetime(draft.expires_at),
    }


def _parse_id(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value).strip())
    except ValueError:
        return None


async def save(
    session: AsyncSession, user_id: uuid.UUID, draft_id: Any, payload: Any, title: Any = None
) -> OrderDraft:
    """Upsert: updates the caller's live draft `draft_id`, otherwise creates a new one (an
    unknown, expired or foreign id never touches another row)."""
    if not isinstance(payload, dict):
        raise DraftRefused(400, "invalid_payload")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
        raise DraftRefused(413, "payload_too_large")
    now = now_utc()
    draft = None
    parsed = _parse_id(draft_id)
    if parsed is not None:
        draft = (
            await session.execute(
                select(OrderDraft)
                .where(OrderDraft.id == parsed, OrderDraft.user_id == user_id, OrderDraft.expires_at > now)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if draft is None:
        draft = OrderDraft(user_id=user_id, created_at=now)
        session.add(draft)
    draft.payload = payload
    draft.title = derive_title(payload, title)
    draft.updated_at = now
    draft.expires_at = now + TTL
    await session.flush()
    await _enforce_cap(session, user_id, keep=draft.id)
    return draft


async def _enforce_cap(session: AsyncSession, user_id: uuid.UUID, keep: uuid.UUID) -> None:
    live = (
        await session.execute(
            select(OrderDraft.id)
            .where(OrderDraft.user_id == user_id, OrderDraft.expires_at > now_utc())
            .order_by(OrderDraft.updated_at.desc(), OrderDraft.id)
        )
    ).scalars()
    extra = [draft_id for draft_id in live if draft_id != keep][MAX_DRAFTS - 1 :]
    if extra:
        await session.execute(delete(OrderDraft).where(OrderDraft.id.in_(extra)))


async def list_live(session: AsyncSession, user_id: uuid.UUID) -> list[OrderDraft]:
    return list(
        (
            await session.execute(
                select(OrderDraft)
                .where(OrderDraft.user_id == user_id, OrderDraft.expires_at > now_utc())
                .order_by(OrderDraft.updated_at.desc(), OrderDraft.id)
                .limit(MAX_DRAFTS)
            )
        ).scalars()
    )


async def remove(session: AsyncSession, user_id: uuid.UUID, draft_id: Any) -> bool:
    """Deletes the caller's draft; False when it is not his (or unknown)."""
    parsed = _parse_id(draft_id)
    if parsed is None:
        return False
    result = await session.execute(
        delete(OrderDraft).where(OrderDraft.id == parsed, OrderDraft.user_id == user_id)
    )
    return (result.rowcount or 0) > 0


async def purge_expired(session: AsyncSession) -> int:
    result = await session.execute(delete(OrderDraft).where(OrderDraft.expires_at <= now_utc()))
    return int(result.rowcount or 0)
