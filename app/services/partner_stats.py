"""The customer badge of partner orders, counted PER PHONE NUMBER (owner, 16/10/2026, point 3).

Partner orders all belong to the restaurant's technical user, so the usual per-account record
(app/services/orders.py reliability_extras) says nothing about the person at the door. Here the
same rules are applied to the orders sharing a `contact_phone_e164` and `source = 'partner'`:
the last RECENT_ORDERS finished orders (delivered, or left with a counted « client ne répond pas »
incident) and how many of them were not paid — wording of 10/10: « n'a pas payé X fois sur ses Y
dernières commandes », never a suspension.

Live values are read from `orders`; `partner_customer_stats` (sha256 of the number) is refreshed
on every terminal status so the badge survives the anonymisation of old orders (30-day job, later).
"""

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NoResponseCase, Order, PartnerCustomerStat
from app.services.orders import INCIDENT_WINDOW_DAYS, RECENT_ORDERS


def phone_hash(phone_e164: str) -> str:
    return hashlib.sha256(phone_e164.strip().encode()).hexdigest()


def _counted() -> Any:
    return (
        select(NoResponseCase.id)
        .where(NoResponseCase.order_id == Order.id, NoResponseCase.incident_counted.is_(True))
        .exists()
    )


def _partner_orders_of(phone: str) -> tuple[Any, ...]:
    return (Order.source == "partner", Order.contact_phone_e164 == phone)


async def recent(session: AsyncSession, phone: str) -> tuple[int, int]:
    """(finished orders among the last RECENT_ORDERS, how many unpaid) for this number."""
    counted = _counted()
    flags = [
        bool(unpaid)
        for (unpaid,) in await session.execute(
            select(counted)
            .where(*_partner_orders_of(phone), or_(Order.status == "delivered", counted))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(RECENT_ORDERS)
        )
    ]
    return len(flags), sum(flags)


async def incidents(session: AsyncSession, phone: str) -> int:
    """Counted no-response incidents of the window on this number's partner orders."""
    since = func.now() - timedelta(days=INCIDENT_WINDOW_DAYS)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(NoResponseCase)
                .join(Order, Order.id == NoResponseCase.order_id)
                .where(
                    *_partner_orders_of(phone),
                    NoResponseCase.incident_counted.is_(True),
                    func.coalesce(NoResponseCase.final_at, NoResponseCase.created_at) >= since,
                )
            )
        ).scalar_one()
    )


async def counts(session: AsyncSession, phone: str) -> tuple[int, int]:
    """(orders of this number, delivered ones), all time."""
    total, delivered = (
        await session.execute(
            select(func.count(), func.count().filter(Order.status == "delivered")).where(
                *_partner_orders_of(phone)
            )
        )
    ).one()
    return int(total), int(delivered)


def times_ar(n: int) -> str:
    if n == 1:
        return "مرة واحدة"
    if n == 2:
        return "مرتين"
    return f"{n} مرات" if n <= 10 else f"{n} مرة"


def orders_ar(n: int) -> str:
    if n == 2:
        return "طلبين"
    if 3 <= n <= 10:
        return f"{n} طلبات"
    return f"{n} {'طلباً' if n >= 11 else 'طلب'}"


def badge_texts(total: int, unpaid: int) -> tuple[str | None, str | None]:
    """The courier's badge (src/components/courier/CustomerWarningBadge.jsx), None when clean."""
    if total <= 0 or unpaid <= 0:
        return None, None
    if total == 1:
        return "Ce client n'a pas payé sa dernière commande", "هذا الحريف لم يدفع ثمن طلبه الأخير"
    times_fr = "1 fois" if unpaid == 1 else f"{unpaid} fois"
    fr = f"Ce client n'a pas payé {times_fr} sur ses {total} dernières commandes"
    ar = f"هذا الحريف لم يدفع {times_ar(unpaid)} من آخر {orders_ar(total)}"
    return fr, ar


async def badge(session: AsyncSession, phone: str | None) -> dict[str, Any]:
    """What POST /orders answers and what the couriers see before offering."""
    total, unpaid = await recent(session, phone) if phone else (0, 0)
    fr, ar = badge_texts(total, unpaid)
    return {
        "recent_orders": total,
        "recent_unpaid": unpaid,
        "visible": unpaid > 0,
        "text_fr": fr,
        "text_ar": ar,
    }


async def reliability(session: AsyncSession, phone: str) -> dict[str, Any]:
    """The getCustomerReliability extras for a partner order, per phone number."""
    total, unpaid = await recent(session, phone)
    _all, delivered = await counts(session, phone)
    window_incidents = await incidents(session, phone)
    denominator = delivered + window_incidents
    return {
        "incidents": window_incidents,
        "delivered_orders": delivered,
        "recent_orders": total,
        "recent_unpaid": unpaid,
        "reliability_pct": round(100 * delivered / denominator) if denominator else 100,
        "avg_reply_seconds": None,  # the restaurant's customer never chats in the app
    }


async def refresh(session: AsyncSession, phone: str | None) -> None:
    """Upserts the number's snapshot row from `orders` (called on terminal statuses)."""
    if not phone:
        return
    total, delivered = await counts(session, phone)
    recent_total, unpaid = await recent(session, phone)
    last = (
        await session.execute(select(func.max(Order.created_at)).where(*_partner_orders_of(phone)))
    ).scalar_one()
    values = {
        "total_orders": total,
        "delivered_orders": delivered,
        "recent_orders": recent_total,
        "recent_unpaid": unpaid,
        "last_order_at": last,
    }
    stmt = pg_insert(PartnerCustomerStat).values(phone_hash=phone_hash(phone), **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["phone_hash"], set_=values))
