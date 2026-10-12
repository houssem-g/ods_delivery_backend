"""Switch the launch mode (app/services/launch_mode.py) in tests."""

from app.db import SessionLocal
from app.models import AppSetting
from app.services import launch_mode


async def set_launch_mode(on: bool) -> None:
    async with SessionLocal() as s:
        row = await s.get(AppSetting, launch_mode.SETTINGS_KEY)
        if row is None:
            s.add(AppSetting(key=launch_mode.SETTINGS_KEY, value={launch_mode.FIELD: on}))
        else:
            row.value = {**(row.value or {}), launch_mode.FIELD: on}
        await s.commit()
