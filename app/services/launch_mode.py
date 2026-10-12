"""« Lancement » (owner, 13/10/2026): while ODS Delivery is short of couriers, EVERY verified,
free courier hears about the new orders around him, even when he is « Hors ligne » or signed out
of the app. Once there are enough couriers, an admin switches it off in Admin → Paramètres:
from then on only the couriers « En ligne » are notified (the normal rule).

Stored in app_settings (key 'dispatch'), so switching needs no deploy. On by default: a database
without the row is in launch mode.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import AppSetting
from app.security.deps import CurrentUser

SETTINGS_KEY = "dispatch"
FIELD = "notify_offline_couriers"
DEFAULT = True


async def notify_offline_couriers(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return bool(value.get(FIELD, DEFAULT))


async def get_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """getDispatchSettings (admin)."""
    if not admin.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    return {"success": True, FIELD: await notify_offline_couriers(session)}


async def set_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """setDispatchSettings (admin): { notify_offline_couriers: true | false }."""
    if not admin.is_admin:
        raise ApiError(403, "forbidden", "Admin only")
    wanted = payload.get(FIELD)
    if not isinstance(wanted, bool):
        raise ApiError(400, "validation_error", f"{FIELD}: true or false")
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    value[FIELD] = wanted
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=value, updated_by=admin.id))
    else:
        row.value, row.updated_by = value, admin.id
    await session.flush()
    return {"success": True, FIELD: wanted}
