"""Shared bits of the order functions (a module starting with _ is not a function)."""

import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.query import get_document
from app.compat.registry import get_entity
from app.security.deps import CurrentUser
from app.services.couriers import ProfileRefused
from app.services.orders import OrderRefused

Result = tuple[int, dict[str, Any]]
Handler = Callable[[dict[str, Any], CurrentUser, AsyncSession, Request], Awaitable[Result]]


def answers_refusals(handler: Handler) -> Handler:
    """A business refusal raised by a service becomes the Deno answer `{error, ...}`."""

    @functools.wraps(handler)
    async def wrapped(
        payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
    ) -> Result:
        try:
            return await handler(payload, user, session, request)
        except (OrderRefused, ProfileRefused) as exc:
            return exc.status, exc.body()

    return wrapped


def as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


async def document(
    session: AsyncSession, entity: str, user: CurrentUser, doc_id: Any
) -> dict[str, Any] | None:
    """The legacy document as `user` may read it (what the Deno function returned)."""
    definition = get_entity(entity)
    assert definition is not None
    return await get_document(session, definition, user, str(doc_id))
