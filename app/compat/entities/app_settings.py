"""AppSettings: one row `key='main'` holding the support numbers (base44/entities/AppSettings.jsonc).

Read: any signed-in user (anonymous screens use the getSupportContacts function).
Create / update / delete: admins. `updated_by` is set by the server, never taken from the body.
The legacy id is the key, so `AppSettings.update('main', …)` works.
"""

from typing import Any

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import AppSetting, User
from app.security.deps import CurrentUser

settings_table = AppSetting.__table__
editor = User.__table__.alias("settings_editor")
VALUE_FIELDS = ("support_phone", "support_whatsapp")
MAX_VALUE_LENGTH = 32


def _require_admin(actor: CurrentUser, operation: str) -> None:
    if not actor.is_admin:
        raise ApiError(
            403, "permission_denied", f"Permission denied for {operation} operation on AppSettings"
        )


def _merge(row: AppSetting, values: dict[str, Any], actor: CurrentUser) -> None:
    merged = dict(row.value or {})
    for name in VALUE_FIELDS:
        if name in values:
            value = (values[name] or "").strip()
            if len(value) > MAX_VALUE_LENGTH:
                raise ApiError(400, "validation_error", f"{name}: at most {MAX_VALUE_LENGTH} characters")
            merged[name] = value
    row.value = merged
    row.updated_by = actor.id


async def create(session: AsyncSession, actor: CurrentUser, data: dict[str, Any]) -> str:
    _require_admin(actor, "create")
    values = coerce_payload(ENTITY, data, {"key", *VALUE_FIELDS})
    key = (values.get("key") or "").strip()
    if not key or len(key) > 64:
        raise ApiError(400, "validation_error", "key: required (at most 64 characters)")
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == key).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=key, value={})
        session.add(row)
    _merge(row, values, actor)
    await session.flush()
    return key


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    _require_admin(actor, "update")
    row = await session.get(AppSetting, doc_id, with_for_update=True)
    if row is None:
        raise ApiError(404, "not_found", "AppSettings not found")
    _merge(row, coerce_payload(ENTITY, data, set(VALUE_FIELDS)), actor)
    await session.flush()


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> None:
    _require_admin(actor, "delete")
    row = await session.get(AppSetting, doc_id)
    if row is None:
        raise ApiError(404, "not_found", "AppSettings not found")
    await session.delete(row)
    await session.flush()


ENTITY = register(
    EntityDef(
        name="AppSettings",
        source=settings_table.outerjoin(editor, editor.c.id == settings_table.c.updated_by),
        id_expr=settings_table.c.key,
        id_type="text",
        created_expr=settings_table.c.created_at,
        updated_expr=settings_table.c.updated_at,
        fields={
            "key": LegacyField(settings_table.c.key, "string"),
            "support_phone": LegacyField(settings_table.c.value["support_phone"].astext, "string"),
            "support_whatsapp": LegacyField(settings_table.c.value["support_whatsapp"].astext, "string"),
            "updated_by": LegacyField(editor.c.email, "string"),
        },
        read_policy=lambda _user: true(),
        create=create,
        update=update,
        delete=delete,
    )
)
