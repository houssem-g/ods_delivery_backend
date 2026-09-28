"""reportOrderIssue — the assigned courier reports a problem while the delivery runs
(base44/functions/reportOrderIssue). Kept in order_issues (Base44 lost them: audit §3.4 bis),
at most 5 per order; the customer and every admin get an in-app notice (issue_reported).

Body: { order_id, courier_id, issue_type, description?, photo_url? } — photo_url must be one of
our public uploads. Returns { success, message }. Errors: missing / invalid (400),
Unauthorized (403), Order not found (404), order_not_active (409), too_many_reports (429).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import OrderIssue, User
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_texts
from app.services import order_transitions as ot
from app.services.notifications import notify
from app.services.orders import courier_of_user

ISSUE_TYPES = (
    "wrong_address",
    "customer_not_available",
    "product_damaged",
    "partial_order",
    "price_different",
    "other",
)
MAX_ISSUES_PER_ORDER = 5


def _photo_key(url: Any) -> str | None:
    prefix = f"{settings.public_files_base_url}/"
    if (
        isinstance(url, str)
        and url.startswith(prefix)
        and url[len(prefix) :].startswith("public/")
        and len(url) <= 500
    ):
        return url[len(prefix) :]
    return None


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id, courier_id, issue_type = (
        payload.get("order_id"),
        payload.get("courier_id"),
        payload.get("issue_type"),
    )
    if not order_id or not issue_type:
        return 400, {"error": "Missing required fields"}
    if issue_type not in ISSUE_TYPES:
        return 400, {"error": "Invalid issue_type"}
    description = (
        payload.get("description").strip()[:1000] if isinstance(payload.get("description"), str) else ""
    )
    photo_key = _photo_key(payload.get("photo_url"))
    order = await ot.lock_order(session, order_id)
    if order is None:
        return 404, {"error": "Order not found"}
    courier = await courier_of_user(session, user.id)
    if courier is None or str(courier.id) != str(courier_id) or order.courier_id != courier.id:
        return 403, {"error": "Unauthorized"}
    if order.status not in ot.LIVE_STATUSES:
        return 409, {"error": "order_not_active"}
    count = (
        await session.execute(
            select(func.count()).select_from(OrderIssue).where(OrderIssue.order_id == order.id)
        )
    ).scalar_one()
    if count >= MAX_ISSUES_PER_ORDER:
        return 429, {"error": "too_many_reports"}
    session.add(
        OrderIssue(
            order_id=order.id,
            reporter_id=user.id,
            issue_type=issue_type,
            description=description,
            photo_key=photo_key,
        )
    )
    await session.flush()
    emit(session, "Order", "update", order.id)
    # In-app only, like the live function (it wrote the Notification rows directly).
    await notify(
        session,
        user_id=order.customer_id,
        type_="issue_reported",
        order_id=order.id,
        push=False,
        metadata={"issue_type": issue_type, "description": description},
        **order_texts.issue_for_customer(issue_type),
    )
    photo_url = f"{settings.public_files_base_url}/{photo_key}" if photo_key else ""
    admins = (
        await session.execute(select(User.id).where(User.role == "admin", User.deleted_at.is_(None)))
    ).scalars()
    for admin_id in admins:
        await notify(
            session,
            user_id=admin_id,
            type_="issue_reported",
            order_id=order.id,
            push=False,
            metadata={
                "issue_type": issue_type,
                "description": description,
                "courier_id": str(courier.id),
                "photo_url": photo_url,
            },
            **order_texts.issue_for_admin(issue_type, str(order.id)[-6:]),
        )
    return 200, {"success": True, "message": "Issue reported successfully"}
