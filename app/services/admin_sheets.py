"""Admin, read only (owner 20/10): the test accounts to leave out of the admin screens, and the
« fiche » of a customer or of a courier — who he is, every delivery, who did it, incidents.

Nothing here changes a row and nothing is done in the person's name: the admin only looks.
Every function refuses a non-admin caller (403) — the sheets carry other people's phone numbers,
addresses and amounts.

Test accounts (« Masquer les comptes et commandes de test »): the QA_ACCOUNTS list, the
example.* / .test addresses (QA campaign qa-*@example.test, Playwright seeds), the
qa- / pw- / e2e- prefixes, and a « QA » / « PW- » display name. A test order: « QA TEST » /
« PW- » in its items (TEST_ORDER_SQL) or placed by a test account. Nothing is deleted: the admin
box only hides them, unticked everything shows again.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ColumnElement, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.config import settings
from app.models import (
    Courier,
    CourierLedgerEntry,
    NoResponseCase,
    Order,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStatusEvent,
    User,
)
from app.security.deps import CurrentUser
from app.services import credit
from app.services.commission import COMMISSION_KINDS, KIND_DUE, LEGACY_STATUS
from app.services.orders import active_incidents, is_test_order, recent_badge
from app.services.super_admin import is_super_admin_email

Result = tuple[int, dict[str, Any]]

ORDERS_MAX = 300  # a sheet lists the latest 300 deliveries (counters are over all of them)
OFFERS_MAX = 100
TEST_EMAIL_SQL = (
    r"@(example\.(test|com|org|net)|[^@]*\.(test|example))$"  # test domains, never @deleted.invalid
    r"|^(qa|pw|e2e|playwright)[-_.+0-9]"  # qa-…, pw-…, e2e… seeds
    r"|^contact\+[^@]*@ordering-dashboard-solution\.com$"  # the owner's contact+…@ trial sign-ups
)
# « QA … », « PW-… », « Test5 », « Testeur-client » (owner 20/10: his test accounts), never « Test User »
TEST_NAME_SQL = r"^\s*((QA|PW)([- ]|$)|Test[0-9]|Testeur)"


def _test_user_sql(users: Any = User) -> ColumnElement[bool]:
    qa = [e.lower() for e in settings.QA_ACCOUNTS]
    return or_(
        func.lower(users.email).in_(qa) if qa else false(),
        users.email.op("~*", return_type=Boolean)(TEST_EMAIL_SQL),
        users.full_name.op("~*", return_type=Boolean)(TEST_NAME_SQL),
    )


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _forbidden(user: CurrentUser | None) -> Result | None:
    if user is None or not user.is_admin:
        return 403, {"error": "Forbidden"}
    return None


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


async def test_accounts(session: AsyncSession, user: CurrentUser | None) -> Result:
    """{ user_ids, courier_ids }: the accounts the admin box hides (couriers by couriers.id)."""
    if (refused := _forbidden(user)) is not None:
        return refused
    rows = (
        await session.execute(
            select(User.id, User.email, Courier.id)
            .outerjoin(Courier, Courier.user_id == User.id)
            .where(_test_user_sql())
        )
    ).all()
    return 200, {
        "success": True,
        "user_ids": sorted({str(u) for u, _, _ in rows}),
        # the legacy screens name a person by e-mail (UserProfile.user_id, Order.customer_id)
        "emails": sorted({str(e).lower() for _, e, _ in rows}),
        "courier_ids": sorted({str(c) for _, _, c in rows if c is not None}),
    }


def _order_view(o: Order, test_customer: bool = False) -> dict[str, Any]:
    fee = o.delivery_fee
    total = None if o.purchase_amount is None else (o.purchase_amount + (fee or 0))
    return {
        "id": str(o.id),
        "created_date": legacy_datetime(o.created_at),
        "delivered_at": legacy_datetime(o.delivered_at),
        "cancelled_at": legacy_datetime(o.cancelled_at),
        "status": str(o.status),
        "fulfillment": o.fulfillment,
        "source": o.source,
        "items_text": o.items_text,
        "purchase_amount": _money(o.purchase_amount),
        "estimated_price": _money(o.estimated_price),
        "delivery_fee": _money(fee),
        "total_amount": _money(total),
        "delivery_address": o.delivery_address,
        "delivery_city": o.delivery_city,
        "cancelled_by": o.cancelled_by,
        "cancel_reason": o.cancel_reason,
        "late_minutes": o.punctuality_late_minutes,
        "is_test_order": is_test_order(o.items_text) or test_customer,
    }


async def _incidents(session: AsyncSession, where: Any) -> list[dict[str, Any]]:
    """No-response cases and reported issues of the orders matching `where`, latest first."""
    cases = (
        await session.execute(
            select(NoResponseCase, Order.courier_id, Order.customer_id)
            .join(Order, Order.id == NoResponseCase.order_id)
            .where(where)
            .order_by(NoResponseCase.started_at.desc())
            .limit(100)
        )
    ).all()
    issues = (
        await session.execute(
            select(OrderIssue, Order.courier_id, Order.customer_id)
            .join(Order, Order.id == OrderIssue.order_id)
            .where(where)
            .order_by(OrderIssue.created_at.desc())
            .limit(100)
        )
    ).all()
    out = [
        {
            "kind": "no_response",
            "order_id": str(c.order_id),
            "date": legacy_datetime(c.started_at),
            "status": c.status,
            "resolution": c.resolution,
            "counted": bool(c.incident_counted),
            "amount": _money(c.purchase_amount),
            "courier_id": str(courier_id) if courier_id else None,
            "customer_id": str(customer_id),
        }
        for c, courier_id, customer_id in cases
    ] + [
        {
            "kind": "issue",
            "order_id": str(i.order_id),
            "date": legacy_datetime(i.created_at),
            "issue_type": i.issue_type,
            "description": (i.description or "")[:300] or None,
            "resolved": i.resolved_at is not None,
            "courier_id": str(courier_id) if courier_id else None,
            "customer_id": str(customer_id),
        }
        for i, courier_id, customer_id in issues
    ]
    return sorted(out, key=lambda r: r["date"] or "", reverse=True)


async def _is_test_user(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return bool(
        (await session.execute(select(_test_user_sql()).where(User.id == user_id))).scalar_one_or_none()
    )


async def _find_user(session: AsyncSession, raw: Any) -> User | None:
    """By users.id, or by e-mail (what the legacy admin lists carry as user_id / customer_id)."""
    uid = _uuid(raw)
    if uid is not None:
        return await session.get(User, uid)
    email = str(raw or "").strip()
    if "@" not in email:
        return None
    return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def customer_sheet(session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any]) -> Result:
    if (refused := _forbidden(user)) is not None:
        return refused
    person = await _find_user(session, payload.get("user_id"))
    if person is None:
        return 404, {"error": "not_found"}
    test_customer = await _is_test_user(session, person.id)
    own_courier = (
        await session.execute(select(Courier).where(Courier.user_id == person.id))
    ).scalar_one_or_none()
    inviter_id = person.referred_by_courier_id
    inviter = await session.get(Courier, inviter_id) if inviter_id else None

    orders = (
        await session.execute(
            select(Order, Courier.display_name)
            .outerjoin(Courier, Courier.id == Order.courier_id)
            .where(Order.customer_id == person.id)
            .order_by(Order.created_at.desc())
            .limit(ORDERS_MAX)
        )
    ).all()
    counts = dict(
        (
            await session.execute(
                select(Order.status, func.count())
                .where(Order.customer_id == person.id)
                .group_by(Order.status)
            )
        ).all()
    )
    recent_total, recent_unpaid = await recent_badge(session, person.id)
    rows = []
    for o, courier_name in orders:
        view = _order_view(o, test_customer)
        view["courier"] = {"id": str(o.courier_id), "name": courier_name} if o.courier_id else None
        rows.append(view)
    return 200, {
        "success": True,
        "customer": {
            "user_id": str(person.id),
            "full_name": person.full_name,
            "phone": person.phone_e164,
            "email": person.email,
            "created_date": legacy_datetime(person.created_at),
            "language": person.language,
            "is_active": person.disabled_at is None and person.deleted_at is None,
            "deleted": person.deleted_at is not None,
            "is_test_account": test_customer,
            "courier_id": str(own_courier.id) if own_courier else None,
            "account_role": person.role,
            "is_super_admin": is_super_admin_email(person.email) and person.role == "admin",
            "invited_by": {"id": str(inviter.id), "name": inviter.display_name} if inviter else None,
        },
        "reliability": {
            "recent_orders": recent_total,
            "recent_unpaid": recent_unpaid,
            "active_incidents": await active_incidents(session, person.id),
        },
        "totals": {
            "orders": sum(int(v) for v in counts.values()),
            "delivered": int(counts.get("delivered", 0)) + int(counts.get("collected", 0)),
            "cancelled": int(counts.get("cancelled", 0)),
        },
        "orders": rows,
        "orders_truncated": len(rows) >= ORDERS_MAX,
        "incidents": await _incidents(session, Order.customer_id == person.id),
    }


async def _find_courier(session: AsyncSession, payload: dict[str, Any]) -> Courier | None:
    cid = _uuid(payload.get("courier_id"))
    if cid is not None:
        return await session.get(Courier, cid)
    account = await _find_user(session, payload.get("user_id"))
    if account is None:
        return None
    return (await session.execute(select(Courier).where(Courier.user_id == account.id))).scalar_one_or_none()


async def courier_sheet(session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any]) -> Result:
    if (refused := _forbidden(user)) is not None:
        return refused
    courier = await _find_courier(session, payload)
    if courier is None:
        return 404, {"error": "not_found"}
    account = await session.get(User, courier.user_id)
    test_courier = await _is_test_user(session, courier.user_id)

    customer = User.__table__.alias("customer")
    orders = (
        await session.execute(
            select(Order, customer.c.full_name, _test_user_sql(customer.c).label("test_customer"))
            .join(customer, customer.c.id == Order.customer_id)
            .where(Order.courier_id == courier.id)
            .order_by(Order.created_at.desc())
            .limit(ORDERS_MAX)
        )
    ).all()
    ids = [o.id for o, _, _ in orders]
    commissions = (
        {
            oid: (kind, amount)
            for oid, kind, amount in (
                await session.execute(
                    select(
                        CourierLedgerEntry.order_id, CourierLedgerEntry.kind, CourierLedgerEntry.amount
                    ).where(
                        CourierLedgerEntry.courier_id == courier.id,
                        CourierLedgerEntry.order_id.in_(ids),
                        CourierLedgerEntry.kind.in_(COMMISSION_KINDS),
                    )
                )
            ).all()
        }
        if ids
        else {}
    )
    accepted = (
        {
            oid: (eta, est)
            for oid, eta, est in (
                await session.execute(
                    select(
                        OrderOffer.order_id, OrderOffer.eta_minutes, OrderOffer.estimated_eta_minutes
                    ).where(
                        OrderOffer.courier_id == courier.id,
                        OrderOffer.order_id.in_(ids),
                        OrderOffer.status == "accepted",
                    )
                )
            ).all()
        }
        if ids
        else {}
    )
    rows = []
    for o, customer_name, test_customer in orders:
        view = _order_view(o, bool(test_customer))
        view["customer"] = {"id": str(o.customer_id), "name": customer_name}
        kind, amount = commissions.get(o.id, (None, None))
        view["commission"] = _money(amount)
        view["commission_status"] = LEGACY_STATUS.get(kind) if kind else None
        due = amount if kind == KIND_DUE else Decimal(0)
        view["earnings"] = (
            _money((o.delivery_fee or Decimal(0)) - due)
            if o.status in ("delivered", "collected") and o.delivery_fee is not None
            else None
        )
        eta, est = accepted.get(o.id, (o.eta_minutes, None))
        view["eta_announced"] = eta
        view["eta_computed"] = est
        rows.append(view)

    offers = (
        await session.execute(
            select(OrderOffer, Order.items_text, Order.status)
            .join(Order, Order.id == OrderOffer.order_id)
            .where(OrderOffer.courier_id == courier.id)
            .order_by(OrderOffer.created_at.desc())
            .limit(OFFERS_MAX)
        )
    ).all()
    drops = (
        await session.execute(
            select(OrderStatusEvent.order_id, OrderStatusEvent.created_at, OrderStatusEvent.reason)
            .where(
                OrderStatusEvent.actor_user_id == courier.user_id,
                OrderStatusEvent.cancelled_by == "courier",
            )
            .order_by(OrderStatusEvent.created_at.desc())
            .limit(100)
        )
    ).all()
    invited = (
        await session.execute(
            select(User.id, User.full_name, User.created_at, func.count(Order.id))
            .outerjoin(Order, Order.customer_id == User.id)
            .where(User.referred_by_courier_id == courier.id)
            .group_by(User.id)
            .order_by(User.created_at.desc())
            .limit(200)
        )
    ).all()
    rating_avg, rating_count = (
        await session.execute(
            select(func.avg(OrderRating.rating), func.count()).where(OrderRating.courier_id == courier.id)
        )
    ).one()
    commission_totals = dict(
        (
            await session.execute(
                select(CourierLedgerEntry.kind, func.sum(CourierLedgerEntry.amount))
                .where(
                    CourierLedgerEntry.courier_id == courier.id,
                    CourierLedgerEntry.kind.in_(COMMISSION_KINDS),
                )
                .group_by(CourierLedgerEntry.kind)
            )
        ).all()
    )
    delivered = [r for r in rows if r["status"] in ("delivered", "collected")]
    return 200, {
        "success": True,
        "courier": {
            "id": str(courier.id),
            "user_id": str(courier.user_id),
            "full_name": courier.display_name,
            "phone": courier.phone_e164,
            "email": account.email if account else None,
            "created_date": legacy_datetime(courier.created_at),
            "verification_status": courier.verification,
            "vehicle_type": courier.vehicle,
            "vehicle_model": courier.vehicle_model,
            "vehicle_plate": courier.vehicle_plate,
            "price_per_km": _money(courier.price_per_km),
            "min_fee": _money(courier.min_fee),
            "is_online": bool(courier.is_online),
            "last_seen_at": legacy_datetime(courier.last_seen_at),
            "late_cancellations": courier.late_cancellations,
            "average_rating": round(float(rating_avg), 2) if rating_avg is not None else None,
            "ratings_count": int(rating_count or 0),
            "credit_balance": _money(await credit.balance(session, courier.id)),
            "commission_due_total": _money(commission_totals.get(KIND_DUE, Decimal(0))),
            "commission_offered_total": _money(
                sum((v for k, v in commission_totals.items() if k != KIND_DUE), Decimal(0))
            ),
            "is_active": bool(account and account.disabled_at is None and account.deleted_at is None),
            "is_test_account": test_courier,
            "account_role": account.role if account else None,
            "is_super_admin": bool(
                account and account.role == "admin" and is_super_admin_email(account.email)
            ),
        },
        "totals": {
            "orders": len(rows),
            "delivered": len(delivered),
            "fees": round(sum(r["delivery_fee"] or 0 for r in delivered), 3),
            "earnings": round(sum(r["earnings"] or 0 for r in delivered), 3),
        },
        "orders": rows,
        "orders_truncated": len(rows) >= ORDERS_MAX,
        "offers": [
            {
                "id": str(of.id),
                "order_id": str(of.order_id),
                "date": legacy_datetime(of.created_at),
                "items_text": items,
                "order_status": str(order_status),
                "proposed_fee": _money(of.proposed_fee),
                "eta_announced": of.eta_minutes,
                "eta_computed": of.estimated_eta_minutes,
                "status": str(of.status),
                "is_test_order": is_test_order(items),
            }
            for of, items, order_status in offers
        ],
        "drops": [
            {"order_id": str(oid), "date": legacy_datetime(at), "reason": reason} for oid, at, reason in drops
        ],
        "invited_clients": [
            {"user_id": str(uid), "full_name": name, "created_date": legacy_datetime(at), "orders": int(n)}
            for uid, name, at, n in invited
        ],
        "incidents": await _incidents(session, Order.courier_id == courier.id),
    }
