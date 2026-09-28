"""/api/entities/{Entity} — the Base44 entity surface, dispatched to the compat registry."""

from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.query import QueryError, get_document, list_documents, parse_query_param
from app.compat.registry import EntityDef, get_entity
from app.db import get_session
from app.errors import ApiError
from app.realtime.events import emit
from app.security.deps import CurrentUser, current_user

router = APIRouter(prefix="/api/entities", tags=["entities"])


def _entity(name: str) -> EntityDef:
    entity = get_entity(name)
    if entity is None:
        raise ApiError(404, "unknown_entity", f"Entity {name} not found")
    return entity


def _denied(operation: str, entity: str) -> ApiError:
    # Base44 wording, which the front's error handling already knows.
    return ApiError(403, "permission_denied", f"Permission denied for {operation} operation on {entity}")


def _project(docs: list[dict[str, Any]], fields: str | None) -> list[dict[str, Any]]:
    """`fields=a,b` keeps those keys (and id)."""
    if not fields:
        return docs
    wanted = {f.strip() for f in fields.split(",") if f.strip()} | {"id"}
    return [{k: v for k, v in doc.items() if k in wanted} for doc in docs]


@router.get("/{name}")
async def list_or_filter(
    name: str,
    q: str | None = None,
    sort: str | None = None,
    limit: int | None = None,
    skip: int | None = None,
    fields: str | None = None,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    entity = _entity(name)
    try:
        docs = await list_documents(session, entity, user, parse_query_param(q), sort, limit, skip)
    except QueryError as exc:
        raise ApiError(400, "invalid_query", str(exc)) from exc
    return _project(docs, fields)


@router.get("/{name}/{doc_id}")
async def get_one(
    name: str,
    doc_id: str,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    doc = await get_document(session, _entity(name), user, doc_id)
    if doc is None:
        raise ApiError(404, "not_found", f"{name} not found")
    return doc


async def _written(
    session: AsyncSession, entity: EntityDef, user: CurrentUser, doc_id: str
) -> dict[str, Any]:
    """The document after a write, as this user may read it."""
    return await get_document(session, entity, user, doc_id) or {"id": doc_id}


@router.post("/{name}", status_code=201)
async def create(
    name: str,
    data: Any = Body(default=None),
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entity = _entity(name)
    if entity.create is None:
        raise _denied("create", name)
    doc_id = await entity.create(session, user, data if isinstance(data, dict) else {})
    emit(session, name, "create", doc_id)
    await session.commit()
    return await _written(session, entity, user, doc_id)


@router.patch("/{name}/{doc_id}")
@router.put("/{name}/{doc_id}")  # the Base44 SDK used PUT
async def update(
    name: str,
    doc_id: str,
    data: Any = Body(default=None),
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entity = _entity(name)
    if entity.update is None:
        raise _denied("update", name)
    await entity.update(session, user, doc_id, data if isinstance(data, dict) else {})
    emit(session, name, "update", doc_id)
    await session.commit()
    return await _written(session, entity, user, doc_id)


@router.delete("/{name}/{doc_id}")
async def delete(
    name: str,
    doc_id: str,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entity = _entity(name)
    if entity.delete is None:
        raise _denied("delete", name)
    audience = await entity.delete(session, user, doc_id)
    emit(session, name, "delete", doc_id, audience=list(audience) if audience is not None else [user.id])
    await session.commit()
    return {"success": True, "id": doc_id}
