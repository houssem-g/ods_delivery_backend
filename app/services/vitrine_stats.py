"""Vitrine counters: visits and poster scans (one per device and day), and the merchant's private
page GET /api/public/vitrine/merchant/{token} (contract §8).

Visits: `vitrine_visits` keeps (shop, Tunis day, kind, visitor) — kind 'view' or 'scan:<code>' — and
the first row of the day bumps `vitrine_stats_daily`; rows older than 40 days are purged (job
`purge_vitrine_visits`). The visitor is an HMAC (JWT_SECRET) of the account id, else the device id
the app keeps, else the address + user agent: never stored in clear.

Orders are counted from `orders` itself (placed / delivered / items amount / top products / hours):
no second copy that could drift. QA orders are left out, unless the Vitrine is in test mode.
"""

import hashlib
import hmac
import re
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, cast, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import Order, Shop, Vitrine, VitrinePoster, VitrineStatDaily, VitrineVisit
from app.security.deps import CurrentUser
from app.services.orders import TEST_ORDER_SQL
from app.services.vitrine import TUNIS, effectively_public, public_allowed, token_hash

VISITS_KEPT_DAYS = 40
_DEVICE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
MONTHS_SHOWN = 3


def today() -> date:
    return datetime.now(UTC).astimezone(TUNIS).date()


def visitor_key(viewer: CurrentUser | None, device_id: Any, client_key: str) -> str:
    if viewer is not None:
        raw = f"user:{viewer.id}"
    elif isinstance(device_id, str) and _DEVICE.match(device_id):
        raw = f"device:{device_id}"
    else:
        raw = f"client:{client_key}"
    return hmac.new(settings.JWT_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


async def record(session: AsyncSession, shop_id: uuid.UUID, kind: str, visitor: str) -> bool:
    """Counts the view / scan once per visitor and day. True when it was new."""
    day = today()
    inserted = (
        await session.execute(
            insert(VitrineVisit)
            .values(shop_id=shop_id, day=day, kind=kind, visitor=visitor)
            .on_conflict_do_nothing()
            .returning(VitrineVisit.visitor)
        )
    ).first()
    if inserted is None:
        return False
    stats = insert(VitrineStatDaily).values(
        shop_id=shop_id,
        day=day,
        views=1 if kind == "view" else 0,
        scans={kind[5:]: 1} if kind.startswith("scan:") else {},
    )
    if kind == "view":
        stats = stats.on_conflict_do_update(
            index_elements=["shop_id", "day"], set_={"views": VitrineStatDaily.views + 1}
        )
    else:
        code = kind[5:]
        current = func.coalesce(cast(VitrineStatDaily.scans[code].astext, Integer), 0)
        stats = stats.on_conflict_do_update(
            index_elements=["shop_id", "day"],
            set_={
                "scans": VitrineStatDaily.scans.op("||")(func.jsonb_build_object(literal(code), current + 1))
            },
        )
    await session.execute(stats)
    return True


async def purge_visits(session: AsyncSession) -> int:
    cutoff = today() - timedelta(days=VISITS_KEPT_DAYS)
    result = await session.execute(delete(VitrineVisit).where(VitrineVisit.day < cutoff))
    return int(result.rowcount or 0)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def recent_months(now: date | None = None) -> list[str]:
    current = now or today()
    out, year, month = [], current.year, current.month
    for _ in range(MONTHS_SHOWN):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


async def month_scans(session: AsyncSession, shop_id: uuid.UUID, month: str | None = None) -> dict[str, int]:
    key = month or recent_months()[0]
    start, end = month_bounds(int(key[:4]), int(key[5:7]))
    totals: Counter[str] = Counter()
    for (scans,) in await session.execute(
        select(VitrineStatDaily.scans).where(
            VitrineStatDaily.shop_id == shop_id, VitrineStatDaily.day >= start, VitrineStatDaily.day < end
        )
    ):
        for code, count in (scans or {}).items():
            totals[code] += int(count or 0)
    return dict(totals)


async def merchant_stats(session: AsyncSession, token: str, month: str | None) -> dict[str, Any]:
    """The merchant's page: his counters for one month, no customer data at all."""
    token = (token or "").strip()
    row = None
    if 20 <= len(token) <= 100:
        row = (
            await session.execute(
                select(Shop, Vitrine)
                .join(Vitrine, Vitrine.shop_id == Shop.id)
                .where(Vitrine.merchant_token_hash == token_hash(token), Vitrine.status != "archived")
                .with_for_update(of=Vitrine)
            )
        ).first()
    if row is None:
        raise ApiError(404, "not_found", "Lien inconnu")
    shop, vitrine = row
    months = recent_months()
    wanted = month or months[0]
    if wanted not in months or not _MONTH.match(wanted):
        raise ApiError(400, "invalid_month", "month: one of " + ", ".join(months), months=months)
    start, end = month_bounds(int(wanted[:4]), int(wanted[5:7]))
    test_mode = not effectively_public(shop, vitrine, await public_allowed(session))

    local_created = func.timezone("Africa/Tunis", Order.created_at)
    conditions = [
        Order.vitrine_shop_id == shop.id,
        local_created >= start,
        local_created < end,
    ]
    if not test_mode:
        conditions.append(Order.items_text.op("!~*")(TEST_ORDER_SQL))
    orders = (
        await session.execute(
            select(Order.status, Order.cart, func.extract("hour", local_created)).where(*conditions)
        )
    ).all()
    placed = len(orders)
    delivered = [o for o in orders if o[0] == "delivered"]
    items_amount = sum(float((o[1] or {}).get("items_total") or 0) for o in delivered)
    top: Counter[str] = Counter()
    for _status, cart, _hour in orders:
        if _status == "cancelled":
            continue
        for line in (cart or {}).get("lines") or []:
            top[str(line.get("name") or "")] += int(line.get("qty") or 0)
    hours = Counter(int(h) for _s, _c, h in orders if h is not None)

    views = (
        await session.execute(
            select(func.coalesce(func.sum(VitrineStatDaily.views), 0)).where(
                VitrineStatDaily.shop_id == shop.id, VitrineStatDaily.day >= start, VitrineStatDaily.day < end
            )
        )
    ).scalar_one()
    scans = await month_scans(session, shop.id, wanted)
    labels = {
        p.code: p.label
        for p in (
            await session.execute(select(VitrinePoster).where(VitrinePoster.shop_id == shop.id))
        ).scalars()
    }
    vitrine.merchant_link_opens += 1
    await session.flush()
    return {
        "shop": {"name": shop.name, "city": shop.city, "source": vitrine.source},
        "test_mode": test_mode,
        "month": wanted,
        "months": months,
        "orders_placed": placed,
        "orders_delivered": len(delivered),
        "items_amount": round(items_amount, 3),
        "views": int(views or 0),
        "scans": [
            {"code": code, "label": labels.get(code, code), "count": count}
            for code, count in sorted(scans.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "top_products": [{"name": name, "qty": qty} for name, qty in top.most_common(3) if name and qty > 0],
        "hours": [{"hour": h, "orders": hours.get(h, 0)} for h in range(24)],
    }
