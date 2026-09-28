"""Liveness (process up) and readiness (database and object storage reachable)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine
from app.storage import s3

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness() -> JSONResponse:
    checks = {"database": await _database_ok(), "storage": await s3.bucket_reachable()}
    ok = all(checks.values())
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "checks": {k: "ok" if v else "down" for k, v in checks.items()},
        },
        status_code=200 if ok else 503,
    )


async def _database_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
