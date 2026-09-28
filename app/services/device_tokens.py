"""Push tokens of the signed-in user (ports of registerDeviceToken / unregisterDeviceToken).

One row per token (`device_tokens.token` is unique; Base44 deduped on sha256(token)).
One device, one signed-in account: registering a token another account registered
(a previous user of the phone who never logged out) moves it to the caller, so that
account stops receiving pushes here (live Base44 deactivated the other rows).
"""

import hashlib
from typing import Any

from sqlalchemy import func, literal_column, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.entities.device_token import ENDPOINT_HASH
from app.models import DeviceToken
from app.realtime.events import emit
from app.security.deps import CurrentUser

Result = tuple[int, dict[str, Any]]
PLATFORMS = ("web", "android", "ios")
PROVIDERS = ("fcm", "webpush")


def endpoint_hash(token: str) -> str:
    """Base44's DeviceToken.endpoint_hash: sha256(token) as hex, first 32 characters."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def detect_platform(user_agent: str | None) -> str:
    """Platform of a device that did not say (the app always sends it)."""
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "android"
    if any(word in ua for word in ("iphone", "ipad", "ipod")):
        return "ios"
    return "web"


async def register(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any], user_agent: str | None = None
) -> Result:
    token = str(payload.get("token") or "").strip()
    raw_platform = str(payload.get("platform") or "").strip().lower()
    platform = raw_platform or detect_platform(user_agent)
    provider = str(payload.get("provider") or "fcm").lower()
    if not token:
        return 400, {"error": "Missing token"}
    # FCM tokens are ~150-300 chars; anything huge or with spaces is not one.
    if len(token) > 4096 or any(ch.isspace() for ch in token):
        return 400, {"error": "Invalid token"}
    if platform not in PLATFORMS:
        return 400, {"error": "Invalid platform"}
    if provider not in PROVIDERS:
        return 400, {"error": "Invalid provider"}
    app_version, device_model = payload.get("app_version"), payload.get("device_model")
    values = {
        "user_id": user.id,
        "token": token,
        "platform": platform,
        "app_version": app_version[:32] if isinstance(app_version, str) else None,
        "device_model": device_model[:128] if isinstance(device_model, str) else None,
        "locale": payload.get("locale") if payload.get("locale") in ("ar", "fr") else None,
        "is_active": True,
        "last_seen_at": func.now(),
        "last_error": None,
        "failure_count": 0,
    }
    stmt = pg_insert(DeviceToken).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["token"], set_={k: stmt.excluded[k] for k in values if k != "token"}
    ).returning(DeviceToken.id, literal_column("(xmax = 0)").label("inserted"))
    row = (await session.execute(stmt)).one()
    emit(session, "DeviceToken", "create" if row.inserted else "update", row.id)
    return 200, {"success": True, "device_token_id": str(row.id)}


async def unregister(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    wanted = str(payload.get("endpoint_hash") or "").strip()
    token = str(payload.get("token") or "").strip()
    if not wanted and token:
        wanted = endpoint_hash(token)
    if not wanted:
        return 400, {"error": "Missing token or endpoint_hash"}
    ids = list(
        (
            await session.execute(
                update(DeviceToken)
                .where(DeviceToken.user_id == user.id, wanted == ENDPOINT_HASH)
                .values(is_active=False)
                .returning(DeviceToken.id)
            )
        ).scalars()
    )
    for token_id in ids:
        emit(session, "DeviceToken", "update", token_id)
    return 200, {"success": True, "deactivated": len(ids)}
