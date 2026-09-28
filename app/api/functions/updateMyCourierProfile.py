"""updateMyCourierProfile — the only way a courier writes his own profile
(base44/functions/updateMyCourierProfile).

Body:
  { action: 'create', fields }  onboarding: verification 'pending', offline; the ID photo is the
                                private upload key (UploadPrivateFile → file_uri) of the caller.
                                Idempotent: an existing profile is answered untouched (existed).
  { action: 'update', fields }  settings, online switch, GPS position.
  { action: 'record_delivery', order_id }  kept for the app: the counters are derived from the
                                delivered orders now, so this only checks and answers.
Returns { success, profile, ignored? }. Errors: invalid_fields / missing_fields (400, fields),
no_courier_profile (404), unknown_action (400).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, as_uuid, document
from app.models import Courier, Order
from app.security.deps import CurrentUser
from app.services.couriers import create_profile, update_profile


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    action = payload.get("action") or "update"
    if action == "create":
        courier, ignored, existed = await create_profile(session, user, payload.get("fields"))
        profile = await document(session, "CourierProfile", user, courier.id)
        if existed:
            return 200, {"success": True, "profile": profile, "existed": True}
        return 200, {"success": True, "profile": profile, "ignored": ignored}

    courier = (
        await session.execute(select(Courier).where(Courier.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if courier is None:
        return 404, {"error": "no_courier_profile"}

    if action == "update":
        ignored = await update_profile(session, courier, payload.get("fields"))
        return 200, {
            "success": True,
            "profile": await document(session, "CourierProfile", user, courier.id),
            "ignored": ignored,
        }

    if action == "record_delivery":
        order_id = payload.get("order_id").strip() if isinstance(payload.get("order_id"), str) else ""
        if not order_id:
            return 400, {"error": "order_id required"}
        oid = as_uuid(order_id)
        order = await session.get(Order, oid) if oid else None
        if order is None:
            return 404, {"error": "Order not found"}
        if order.courier_id != courier.id:
            return 403, {"error": "Forbidden"}
        if order.status != "delivered":
            return 409, {"error": "order_not_delivered"}
        # total_deliveries / total_earnings are the courier_stats view: nothing to record.
        return 200, {
            "success": True,
            "profile": await document(session, "CourierProfile", user, courier.id),
            "already_recorded": True,
        }

    return 400, {"error": "unknown_action"}
