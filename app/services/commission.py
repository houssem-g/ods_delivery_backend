"""ODS commission ledger and weekly statements.

Business model (ods-delivery src/constants/commission.js, src/lib/commission.js):
- a delivered order with a delivery fee carries the nominal commission
  (COMMISSION_PER_DELIVERY_TND); no fee, no commission and no quota used;
- delivered before LAUNCH_END_DATE (midnight 1 January 2027, Tunis): waived
  ('commission_waived_launch', legacy status 'offered_launch');
- afterwards the first FREE_DELIVERIES_PER_MONTH deliveries of the courier's
  calendar month (Africa/Tunis) are waived ('commission_waived_quota',
  'free_quota'), each one beyond owes the commission ('commission_due', 'due').
Waived entries keep the nominal amount (the ledger says what was offered).
Nothing is deducted at delivery: the due entries are grouped each Monday
(04:00 Tunis) into a statement per courier for the previous week(s).
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, CourierLedgerEntry, CourierStatement, Order

COMMISSION_PER_DELIVERY_TND = Decimal("0.500")
FREE_DELIVERIES_PER_MONTH = 20
LAUNCH_FREE = True
TUNIS = ZoneInfo("Africa/Tunis")
LAUNCH_END_DATE = datetime(2027, 1, 1, tzinfo=TUNIS)

KIND_LAUNCH = "commission_waived_launch"
KIND_QUOTA = "commission_waived_quota"
KIND_DUE = "commission_due"
COMMISSION_KINDS = (KIND_LAUNCH, KIND_QUOTA, KIND_DUE)
# courier_ledger_entries.kind -> legacy Order.ods_commission_status
LEGACY_STATUS = {KIND_LAUNCH: "offered_launch", KIND_QUOTA: "free_quota", KIND_DUE: "due"}


def is_launch_offered(delivered_at: datetime) -> bool:
    return LAUNCH_FREE and delivered_at < LAUNCH_END_DATE


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """[first instant, first instant of next month) of `moment`'s calendar month in Tunis."""
    local = moment.astimezone(TUNIS)
    start = datetime(local.year, local.month, 1, tzinfo=TUNIS)
    nxt = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=TUNIS)
    return start, nxt


async def record_delivery(
    session: AsyncSession, order: Order, delivered_at: datetime
) -> CourierLedgerEntry | None:
    """Writes the order's commission entry (once). Called by the transition to 'delivered'.

    The courier row is locked so two deliveries of the same courier rank one after the other.
    """
    if order.courier_id is None or not (order.delivery_fee and order.delivery_fee > 0):
        return None
    existing = (
        await session.execute(
            select(CourierLedgerEntry).where(
                CourierLedgerEntry.order_id == order.id, CourierLedgerEntry.kind.in_(COMMISSION_KINDS)
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    await session.execute(select(Courier.id).where(Courier.id == order.courier_id).with_for_update())
    if is_launch_offered(delivered_at):
        kind = KIND_LAUNCH
    else:
        start, end = month_bounds(delivered_at)
        rank = (
            await session.execute(
                select(func.count())
                .select_from(CourierLedgerEntry)
                .join(Order, Order.id == CourierLedgerEntry.order_id)
                .where(
                    CourierLedgerEntry.courier_id == order.courier_id,
                    CourierLedgerEntry.kind.in_(COMMISSION_KINDS),
                    Order.delivered_at >= start,
                    Order.delivered_at < end,
                )
            )
        ).scalar_one()
        kind = KIND_QUOTA if rank < FREE_DELIVERIES_PER_MONTH else KIND_DUE
    entry = CourierLedgerEntry(
        courier_id=order.courier_id, order_id=order.id, kind=kind, amount=COMMISSION_PER_DELIVERY_TND
    )
    session.add(entry)
    await session.flush()
    return entry


def week_start(moment: datetime) -> date:
    """Monday (Tunis) of `moment`'s week."""
    local = moment.astimezone(TUNIS).date()
    return local - timedelta(days=local.weekday())


def _tunis_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=TUNIS)


async def build_statements(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Groups every due entry of a finished week (before this Monday, Tunis) that is on no statement
    yet into one statement per courier and week. Idempotent: a second run finds nothing to group;
    a missed week is caught up by the next run."""
    now = now or datetime.now(UTC)
    current_week = _tunis_midnight(week_start(now))
    entries = list(
        (
            await session.execute(
                select(CourierLedgerEntry)
                .where(
                    CourierLedgerEntry.kind == KIND_DUE,
                    CourierLedgerEntry.statement_id.is_(None),
                    CourierLedgerEntry.created_at < current_week,
                )
                .order_by(CourierLedgerEntry.id)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    groups: dict[tuple[uuid.UUID, date], list[CourierLedgerEntry]] = {}
    for entry in entries:
        groups.setdefault((entry.courier_id, week_start(entry.created_at)), []).append(entry)
    created = 0
    for (courier_id, start), rows in sorted(groups.items(), key=lambda item: (item[0][1], str(item[0][0]))):
        statement = (
            await session.execute(
                select(CourierStatement).where(
                    CourierStatement.courier_id == courier_id, CourierStatement.period_start == start
                )
            )
        ).scalar_one_or_none()
        total = sum((r.amount for r in rows), Decimal("0"))
        if statement is None:
            statement = CourierStatement(
                courier_id=courier_id,
                period_start=start,
                period_end=start + timedelta(days=6),
                total_due=total,
                status="open",
            )
            session.add(statement)
            created += 1
        else:  # an entry written late for an already grouped week
            statement.total_due = statement.total_due + total
        await session.flush()
        await session.execute(
            update(CourierLedgerEntry)
            .where(CourierLedgerEntry.id.in_([r.id for r in rows]))
            .values(statement_id=statement.id)
        )
    return {"statements_created": created, "entries_grouped": len(entries)}
