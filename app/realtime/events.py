"""Publishing entity change events.

`emit()` only records the event on the session. Just before the transaction
commits, the recorded events are sent with `pg_notify` inside that same
transaction: PostgreSQL delivers a NOTIFY only if (and when) the transaction
commits, so listeners never see an event for a rolled-back change and an event
can't be lost between the commit and a separate publish.
"""

import json
import uuid
from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings

EventType = Literal["create", "update", "delete"]
_PENDING = "odsd_realtime_pending"


def emit(
    session: AsyncSession | Session,
    entity: str,
    type_: EventType,
    id_: str | uuid.UUID,
    audience: Iterable[uuid.UUID | str] | None = None,
) -> None:
    """Queue `{entity, type, id}` for delivery after commit.

    `audience` (user ids) restricts who receives a *delete* event, whose row can no
    longer be checked against the read policy; admins always receive it.
    """
    info = session.info
    payload: dict[str, Any] = {"entity": entity, "type": type_, "id": str(id_)}
    if audience is not None:
        payload["audience"] = sorted({str(u) for u in audience})
    pending: list[dict[str, Any]] = info.setdefault(_PENDING, [])
    if payload not in pending:
        pending.append(payload)


def pending_events(session: AsyncSession | Session) -> list[dict[str, Any]]:
    return list(session.info.get(_PENDING, []))


@event.listens_for(Session, "before_commit")
def _publish(session: Session) -> None:
    events = session.info.pop(_PENDING, None)
    if not events:
        return
    for payload in events:
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": settings.REALTIME_CHANNEL, "payload": json.dumps(payload, separators=(",", ":"))},
        )


@event.listens_for(Session, "after_rollback")
def _discard(session: Session) -> None:
    session.info.pop(_PENDING, None)
