"""POST /api/functions/{name} — the legacy `functions.invoke(name, payload)` surface.

The body is the payload the front sends today; the answer is the JSON (and status)
the front expects. The handler's writes are committed when it answers
< 400, rolled back otherwise.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions import FUNCTIONS, RETIRED
from app.db import get_session
from app.security.deps import CurrentUser, optional_user

router = APIRouter(prefix="/api/functions", tags=["functions"])
log = logging.getLogger("odsd.functions")


async def _payload(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.post("/{name}")
async def invoke(
    name: str,
    request: Request,
    user: CurrentUser | None = Depends(optional_user),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if name in RETIRED:
        return JSONResponse({"error": "gone", "function": name}, status_code=410)
    function = FUNCTIONS.get(name)
    if function is None:
        # snake_case `error` required: the front treats a 404 without it as "not deployed yet".
        return JSONResponse(
            {"error": "function_not_found", "message": f"Function {name} not found"}, status_code=404
        )
    if function.auth == "user" and user is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    status, body = await function.handle(await _payload(request), user, session, request)
    if status < 400:
        await session.commit()
    else:
        await session.rollback()
        # Why a function was refused, to diagnose a user report ("my order doesn't go
        # through"): the name, the status and the error code only, never payload values.
        raw_error = body.get("error") if isinstance(body, dict) else None
        error = str(raw_error)[:80] if raw_error is not None else "-"
        log.info(
            "function %s refused %s %s",
            name,
            status,
            error,
            extra={"function": name, "status": status, "error": error},
        )
    return JSONResponse(body, status_code=status)
