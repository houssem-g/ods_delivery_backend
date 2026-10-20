"""Super admin (owner 20/10/2026): « seulement moi, contact@ordering-dashboard-solution.com, je peux
ajouter ou supprimer des admins ».

- A super admin = an e-mail of settings.SUPER_ADMIN_EMAILS (any case) whose account is admin, neither
  disabled nor deleted. The list lives in the server settings only: nothing in the app edits it.
- setUserAdmin is the ONLY way to give or remove the admin role (UserProfile refuses 'admin', sign-up
  and Google create customers). Any other caller, other admins included, gets 403.
- A super admin can neither remove himself nor another super admin.
- Removing admin gives back the role the person had before (from the audit log), else « customer ».
- Every change signs the person out of her other sessions (refresh tokens, fingerprint sign-ins); her
  current access token keeps working but the role is read from the database on each request, so the
  new rights apply at once.
- One audit_log row per change: who, when, on whom, role before / after.
"""

import ipaddress
import logging
import uuid
from typing import Any

from fastapi import Request
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AuditLog, User
from app.security.deps import CurrentUser
from app.security.tokens import revoke_all_for_user

log = logging.getLogger("odsd.super_admin")

Result = tuple[int, dict[str, Any]]
GRANTED, REVOKED = "admin_granted", "admin_revoked"
FALLBACK_ROLE = "customer"


def is_super_admin_email(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in {e.strip().lower() for e in settings.SUPER_ADMIN_EMAILS}


def is_super_admin(user: User | None) -> bool:
    return bool(
        user is not None
        and user.role == "admin"
        and user.deleted_at is None
        and user.disabled_at is None
        and is_super_admin_email(user.email)
    )


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ip(request: Request | None) -> str | None:
    if request is None:
        return None
    try:
        return str(ipaddress.ip_address(get_remote_address(request)))
    except ValueError:
        return None


async def _role_before_grant(session: AsyncSession, user_id: uuid.UUID) -> str:
    last = (
        await session.execute(
            select(AuditLog.before)
            .where(AuditLog.entity == "User", AuditLog.entity_id == str(user_id), AuditLog.action == GRANTED)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    role = (last or {}).get("role")
    return role if role in ("customer", "courier") else FALLBACK_ROLE


async def set_user_admin(
    session: AsyncSession, actor: CurrentUser | None, payload: dict[str, Any], request: Request | None
) -> Result:
    if actor is None:
        return 401, {"error": "auth_required"}
    me = await session.get(User, actor.id)
    if not is_super_admin(me):
        return 403, {"error": "forbidden", "message": "Super admin only"}
    assert me is not None
    target_id = _uuid(payload.get("user_id"))
    admin = payload.get("admin")
    if target_id is None or not isinstance(admin, bool):
        return 400, {
            "error": "invalid_payload",
            "message": "user_id (uuid) and admin (true/false) are required",
        }
    target = await session.get(User, target_id, with_for_update=True)
    if target is None or target.deleted_at is not None:
        return 404, {"error": "not_found"}
    before = target.role
    if admin:
        if before == "admin":
            return 200, {"success": True, "changed": False, "role": "admin"}
        if target.disabled_at is not None:
            return 409, {"error": "account_disabled", "message": "Reactivate the account first"}
        target.role = "admin"
    else:
        if target.id == me.id:
            return 409, {"error": "cannot_revoke_self"}
        if is_super_admin_email(target.email):
            return 409, {"error": "cannot_revoke_super_admin"}
        if before != "admin":
            return 200, {"success": True, "changed": False, "role": before}
        target.role = await _role_before_grant(session, target.id)
    await revoke_all_for_user(session, target.id)
    session.add(
        AuditLog(
            actor_user_id=me.id,
            action=GRANTED if admin else REVOKED,
            entity="User",
            entity_id=str(target.id),
            before={"role": before, "email": target.email},
            after={"role": target.role, "by": me.email},
            ip=_ip(request),
        )
    )
    await session.flush()
    log.warning(
        "admin role %s: %s -> %s by %s",
        "granted" if admin else "revoked",
        target.email,
        target.role,
        me.email,
    )
    return 200, {"success": True, "changed": True, "role": target.role}
