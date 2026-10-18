"""getMyPunctuality — the courier's own punctuality (owner, 18/10/2026): on his last WINDOW delivered
courses, how many arrived on time against the time he announced (accepted_at + his delay, kept on the
order when delivered: orders.punctuality_late_minutes) and his average lateness. For HIM only: never
shown to customers, never used in the ranking (for now).

Body: {}. Returns { success, window, count, on_time, avg_late_minutes, tolerance_minutes } — `on_time`:
delivered at most TOLERANCE minutes after the announced time; `avg_late_minutes`: the mean of the
minutes late (an early course counts 0) over those courses. count 0 = nothing measured yet.
Errors: courier_profile_missing (403).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order
from app.security.deps import CurrentUser
from app.services.orders import courier_of_user

WINDOW = 10
TOLERANCE = 5  # minutes after the announced time still « à l'heure »


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        return 403, {"error": "courier_profile_missing"}
    late = list(
        (
            await session.execute(
                select(Order.punctuality_late_minutes)
                .where(
                    Order.courier_id == courier.id,
                    Order.status == "delivered",
                    Order.punctuality_late_minutes.is_not(None),
                )
                .order_by(Order.delivered_at.desc())
                .limit(WINDOW)
            )
        ).scalars()
    )
    return 200, {
        "success": True,
        "window": WINDOW,
        "count": len(late),
        "on_time": sum(1 for m in late if m <= TOLERANCE),
        "avg_late_minutes": round(sum(max(0, m) for m in late) / len(late)) if late else None,
        "tolerance_minutes": TOLERANCE,
    }
