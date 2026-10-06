"""Courier prepaid credit (decision D-10, 06/10/2026): the ODS commission is paid IN ADVANCE.

Why: a weekly statement paid after the fact is spent before it is settled. The courier now tops up a
credit first; every delivery that owes the commission (the 21st of the month onwards, from
CREDIT_ENFORCED_FROM) takes COMMISSION_PER_DELIVERY_TND from it (the `commission_due` ledger entry
written by app/services/commission.py at delivery). With the month's free deliveries used up and less
than one commission left, he can no longer send offers (checked in app/services/offers.py, never in
the middle of a delivery).

Money lives in courier_ledger_entries, sign convention of that table: positive = owed to ODS,
negative = paid / credit. So   credit = -SUM(amount)   over BALANCE_KINDS (waived entries keep their
nominal amount and are left out).

Top-ups (no online payment at first, owner's decision):
  - bank_deposit: cash paid at a bank counter on the ODS account; the courier photographs the receipt
    (requestCreditTopup) and an admin approves it (reviewCreditTopup) -> credit_topup + credit_bonus;
  - cashier: cash handed to a cashier (the ODS café in Sousse), who credits him at once
    (cashierCreditTopup); an admin later marks that cash as handed over (markCashierRemitted).
Bonus (TOPUP_BONUS): 10 DT -> +1 DT, 20 DT -> +3 DT. Primes (fondateur, recrutement) are paid in credit
(grantCourierCredit, kind credit_prime). Nothing is refundable in cash.

December 2026 is the "à blanc" month: the launch waives every commission (commission.py), and the
summary shows what the courier WOULD have paid beyond his 20 free deliveries.
"""

import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import AppSetting, Courier, CourierLedgerEntry, CreditCashier, CreditTopup, File, Order, User
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.commission import (
    COMMISSION_KINDS,
    COMMISSION_PER_DELIVERY_TND,
    FREE_DELIVERIES_PER_MONTH,
    KIND_DUE,
    LAUNCH_END_DATE,
    month_bounds,
)
from app.services.notifications import notify
from app.services.orders import OrderRefused, courier_of_user
from app.services.phones import to_e164
from app.storage import keys, s3

CREDIT_ENFORCED_FROM = LAUNCH_END_DATE  # 1 January 2027, Tunis: before it every commission is waived
BALANCE_KINDS = (KIND_DUE, "payment_received", "adjustment", "credit_topup", "credit_bonus", "credit_prime")
CREDIT_KINDS = ("credit_topup", "credit_bonus", "credit_prime")
TOPUP_BONUS = {Decimal("10"): Decimal("1"), Decimal("20"): Decimal("3")}
TOPUP_MIN = Decimal("5")
TOPUP_MAX = Decimal("200")
SUGGESTED_TOPUPS = (Decimal("10"), Decimal("20"))
LOW_CREDIT_DELIVERIES = 3  # the alert fires when the credit falls under 3 commissions
MAX_PENDING_TOPUPS = 3
PRIME_MAX = Decimal("100")
ADJUST_MAX = Decimal("500")
NOTE_MAX = 300
REFERENCE_MAX = 60
HISTORY_LIMIT = 30
ADMIN_LIST_MAX = 200
SIGNED_SECONDS = 300
RECEIPT_TYPES = keys.IMAGE_TYPES
SETTINGS_KEY = "credit"
SETTINGS_FIELDS = ("bank_name", "account_holder", "rib", "instructions_fr", "instructions_ar")
ZERO = Decimal("0")

LABELS = {
    KIND_DUE: ("Commission ODS", "عمولة ODS"),
    "payment_received": ("Paiement reçu", "دفعة مستلمة"),
    "adjustment": ("Correction", "تعديل"),
    "credit_topup": ("Recharge", "شحن الرصيد"),
    "credit_bonus": ("Bonus de recharge", "مكافأة الشحن"),
    "credit_prime": ("Prime ODS", "منحة ODS"),
}


def _dt(value: Decimal) -> str:
    """12.500 — the amount format of the app ("12.500 DT")."""
    return f"{value.quantize(Decimal('0.001'))}"


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def _amount(value: Any, *, low: Decimal, high: Decimal, error: str, whole: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise OrderRefused(400, error) from None
    if not amount.is_finite() or amount < low or amount > high:
        raise OrderRefused(400, error, min=float(low), max=float(high))
    if whole and amount != amount.to_integral_value():
        raise OrderRefused(400, error, min=float(low), max=float(high))
    return amount.quantize(Decimal("0.001"))


def _note(value: Any, limit: int = NOTE_MAX) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OrderRefused(400, "invalid_note")
    text = " ".join(value.split())[:limit]
    return text or None


def bonus_for(amount: Decimal) -> Decimal:
    return TOPUP_BONUS.get(amount, ZERO)  # equal Decimals hash alike: 10.000 finds 10


def is_enforced(now: datetime | None = None) -> bool:
    return (now or ot.now_utc()) >= CREDIT_ENFORCED_FROM


async def balance(session: AsyncSession, courier_id: uuid.UUID) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(CourierLedgerEntry.amount), 0)).where(
                CourierLedgerEntry.courier_id == courier_id, CourierLedgerEntry.kind.in_(BALANCE_KINDS)
            )
        )
    ).scalar_one()
    return -Decimal(total)


async def month_deliveries(session: AsyncSession, courier_id: uuid.UUID, now: datetime | None = None) -> int:
    """Deliveries of the courier's current calendar month (Tunis) that carried a commission entry
    (a delivery without fee carries none and uses no free delivery), like commission.record_delivery."""
    start, end = month_bounds(now or ot.now_utc())
    return (
        await session.execute(
            select(func.count())
            .select_from(CourierLedgerEntry)
            .join(Order, Order.id == CourierLedgerEntry.order_id)
            .where(
                CourierLedgerEntry.courier_id == courier_id,
                CourierLedgerEntry.kind.in_(COMMISSION_KINDS),
                Order.delivered_at >= start,
                Order.delivered_at < end,
            )
        )
    ).scalar_one()


async def offer_allowed(session: AsyncSession, courier: Courier, now: datetime | None = None) -> dict[str, Any] | None:
    """None when the courier may send an offer; else the refusal details (error `credit_empty`)."""
    now = now or ot.now_utc()
    if not is_enforced(now):
        return None
    used = await month_deliveries(session, courier.id, now)
    if used < FREE_DELIVERIES_PER_MONTH:
        return None
    credit = await balance(session, courier.id)
    if credit >= COMMISSION_PER_DELIVERY_TND:
        return None
    return {"credit": _dt(credit), "free_left": 0, "commission": _dt(COMMISSION_PER_DELIVERY_TND)}


async def settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    return {field: value.get(field) for field in SETTINGS_FIELDS}


async def cashiers_public(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(CreditCashier).where(CreditCashier.active.is_(True)).order_by(CreditCashier.created_at)
        )
    ).scalars()
    return [{"label": r.label, "address": r.address} for r in rows]


def _entry_view(entry: CourierLedgerEntry) -> dict[str, Any]:
    label_fr, label_ar = LABELS.get(entry.kind, (entry.kind, entry.kind))
    return {
        "id": entry.id,
        "kind": entry.kind,
        "label_fr": label_fr,
        "label_ar": label_ar,
        "amount": _dt(-entry.amount),  # signed from the courier's side: + credit, - commission
        "order_id": str(entry.order_id) if entry.order_id else None,
        "created_date": legacy_datetime(entry.created_at),
    }


def topup_view(t: CreditTopup) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "courier_id": str(t.courier_id),
        "method": t.method,
        "status": t.status,
        "amount": _dt(t.amount),
        "bonus": _dt(t.bonus),
        "reference": t.reference,
        "note": t.note,
        "has_receipt": t.receipt_key is not None,
        "reviewed_at": legacy_datetime(t.reviewed_at) if t.reviewed_at else None,
        "remitted_at": legacy_datetime(t.remitted_at) if t.remitted_at else None,
        "created_date": legacy_datetime(t.created_at),
    }


async def _courier(session: AsyncSession, user: CurrentUser) -> Courier:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    return courier


async def summary(session: AsyncSession, user: CurrentUser) -> dict[str, Any]:
    """getMyCredit: what the courier's "Mon crédit" page shows."""
    courier = await _courier(session, user)
    now = ot.now_utc()
    credit = await balance(session, courier.id)
    used = await month_deliveries(session, courier.id, now)
    free_left = max(0, FREE_DELIVERIES_PER_MONTH - used)
    enforced = is_enforced(now)
    covered = int((max(credit, ZERO) / COMMISSION_PER_DELIVERY_TND).to_integral_value(rounding=ROUND_DOWN))
    blocked = await offer_allowed(session, courier, now) is not None
    history = (
        await session.execute(
            select(CourierLedgerEntry)
            .where(CourierLedgerEntry.courier_id == courier.id, CourierLedgerEntry.kind.in_(BALANCE_KINDS))
            .order_by(CourierLedgerEntry.created_at.desc(), CourierLedgerEntry.id.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    pending = (
        await session.execute(
            select(CreditTopup)
            .where(CreditTopup.courier_id == courier.id, CreditTopup.status == "pending")
            .order_by(CreditTopup.created_at.desc())
        )
    ).scalars()
    beyond = max(0, used - FREE_DELIVERIES_PER_MONTH)
    return {
        "success": True,
        "credit": _dt(credit),
        "deliveries_covered": covered,
        "commission": _dt(COMMISSION_PER_DELIVERY_TND),
        "free_per_month": FREE_DELIVERIES_PER_MONTH,
        "month_deliveries": used,
        "free_left": free_left,
        "enforced": enforced,
        "enforced_from": legacy_datetime(CREDIT_ENFORCED_FROM),
        # December "à blanc": what the month WOULD cost once the launch offer ends.
        "would_have_paid": None if enforced else _dt(COMMISSION_PER_DELIVERY_TND * beyond),
        "blocked": blocked,
        "low": enforced and free_left == 0 and covered < LOW_CREDIT_DELIVERIES,
        "suggested_topups": [{"amount": _dt(a), "bonus": _dt(TOPUP_BONUS.get(a, ZERO))} for a in SUGGESTED_TOPUPS],
        "topup_min": _dt(TOPUP_MIN),
        "topup_max": _dt(TOPUP_MAX),
        "pending_topups": [topup_view(t) for t in pending],
        "history": [_entry_view(e) for e in history],
        "where": {"bank": await settings(session), "cashiers": await cashiers_public(session)},
    }


async def _admins(session: AsyncSession) -> list[uuid.UUID]:
    return list(
        (await session.execute(select(User.id).where(User.role == "admin", User.deleted_at.is_(None)))).scalars()
    )


async def request_topup(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """requestCreditTopup: the courier says he deposited cash at a bank counter, with the receipt photo."""
    courier = await _courier(session, user)
    amount = _amount(payload.get("amount"), low=TOPUP_MIN, high=TOPUP_MAX, error="invalid_amount", whole=True)
    raw = payload.get("receipt_url") or payload.get("file_url") or payload.get("file_uri")
    if not isinstance(raw, str) or len(raw) > 512 or not raw.startswith("private/"):
        raise OrderRefused(400, "invalid_receipt")
    file = (await session.execute(select(File).where(File.key == raw).with_for_update())).scalar_one_or_none()
    if (
        file is None
        or file.owner_id != user.id
        or file.visibility != "private"
        or file.content_type not in RECEIPT_TYPES
        or file.purpose in keys.ADMIN_SIGNED_PURPOSES - {"credit_receipt"}
    ):
        raise OrderRefused(400, "invalid_receipt")
    if (
        await session.execute(select(func.count()).select_from(CreditTopup).where(CreditTopup.receipt_key == raw))
    ).scalar_one():
        raise OrderRefused(409, "receipt_already_used")
    pending = (
        await session.execute(
            select(func.count())
            .select_from(CreditTopup)
            .where(CreditTopup.courier_id == courier.id, CreditTopup.status == "pending")
        )
    ).scalar_one()
    if pending >= MAX_PENDING_TOPUPS:
        raise OrderRefused(429, "too_many_pending_topups", max=MAX_PENDING_TOPUPS)
    reference = _note(payload.get("reference"), REFERENCE_MAX)
    topup = CreditTopup(
        courier_id=courier.id,
        method="bank_deposit",
        status="pending",
        amount=amount,
        bonus=ZERO,
        receipt_key=file.key,
        reference=reference,
    )
    file.purpose = "credit_receipt"  # signed for admins only from now on
    session.add(topup)
    await session.flush()
    for admin_id in await _admins(session):
        await notify(
            session,
            user_id=admin_id,
            type_="credit_topup_pending",
            push=False,
            title_fr="💳 Recharge à valider",
            title_ar="💳 شحن للمراجعة",
            body_fr=f"{courier.display_name} : versement de {_dt(amount)} DT. À valider dans Admin › Crédits.",
            body_ar=f"{courier.display_name}: إيداع {_dt(amount)} د.ت. للمراجعة في الإدارة › الأرصدة.",
            metadata={"topup_id": str(topup.id), "recipient_role": "admin"},
        )
    return {"success": True, "topup": topup_view(topup)}


async def _credit(
    session: AsyncSession, courier_id: uuid.UUID, amount: Decimal, bonus: Decimal, by: uuid.UUID
) -> None:
    session.add(CourierLedgerEntry(courier_id=courier_id, kind="credit_topup", amount=-amount, created_by=by))
    if bonus > 0:
        session.add(CourierLedgerEntry(courier_id=courier_id, kind="credit_bonus", amount=-bonus, created_by=by))
    await session.flush()


async def _tell_courier(session: AsyncSession, courier: Courier, topup: CreditTopup) -> None:
    approved = topup.status == "approved"
    credit = await balance(session, courier.id)
    if approved:
        total = topup.amount + topup.bonus
        extra_fr = f" (dont {_dt(topup.bonus)} DT de bonus)" if topup.bonus > 0 else ""
        extra_ar = f" (منها {_dt(topup.bonus)} د.ت مكافأة)" if topup.bonus > 0 else ""
        body_fr = f"+{_dt(total)} DT{extra_fr}. Votre crédit : {_dt(credit)} DT."
        body_ar = f"+{_dt(total)} د.ت{extra_ar}. رصيدك: {_dt(credit)} د.ت."
    else:
        reason_fr = f" : {topup.note}" if topup.note else ""
        reason_ar = f": {topup.note}" if topup.note else ""
        body_fr = f"Recharge de {_dt(topup.amount)} DT refusée{reason_fr}."
        body_ar = f"تم رفض شحن {_dt(topup.amount)} د.ت{reason_ar}."
    await notify(
        session,
        user_id=courier.user_id,
        type_="credit_topup_approved" if approved else "credit_topup_rejected",
        title_fr="✅ Crédit rechargé" if approved else "❌ Recharge refusée",
        title_ar="✅ تم شحن الرصيد" if approved else "❌ تم رفض الشحن",
        body_fr=body_fr,
        body_ar=body_ar,
        metadata={"recipient_role": "courier", "topup_id": str(topup.id), "credit": _dt(credit)},
    )


async def review_topup(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """reviewCreditTopup: approve (optionally with the amount actually read on the receipt) or reject."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    topup_id = _uuid(payload.get("id"))
    status = payload.get("status")
    if status not in ("approved", "rejected"):
        raise OrderRefused(400, "invalid_status")
    note = _note(payload.get("note"))
    topup = (
        (await session.execute(select(CreditTopup).where(CreditTopup.id == topup_id).with_for_update()))
        .scalar_one_or_none()
        if topup_id
        else None
    )
    if topup is None:
        raise OrderRefused(404, "topup_not_found")
    if topup.status != "pending":
        raise OrderRefused(409, "topup_already_reviewed", status=topup.status)
    if status == "approved":
        if payload.get("amount") not in (None, ""):
            topup.amount = _amount(payload.get("amount"), low=Decimal("0.5"), high=TOPUP_MAX, error="invalid_amount")
        topup.bonus = bonus_for(topup.amount)
        await _credit(session, topup.courier_id, topup.amount, topup.bonus, admin.id)
    topup.status, topup.note = status, note
    topup.reviewed_by, topup.reviewed_at = admin.id, ot.now_utc()
    await session.flush()
    courier = await session.get(Courier, topup.courier_id)
    if courier is not None:
        await _tell_courier(session, courier, topup)
    return {"success": True, "topup": topup_view(topup)}


async def list_topups(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """listCreditTopups (admin): newest first, filtered by status / method / courier, signed receipts."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    stmt = (
        select(CreditTopup, Courier.display_name, Courier.phone_e164)
        .join(Courier, Courier.id == CreditTopup.courier_id)
        .order_by(CreditTopup.created_at.desc(), CreditTopup.id)
        .limit(ADMIN_LIST_MAX)
    )
    if payload.get("status") not in (None, ""):
        if payload.get("status") not in ("pending", "approved", "rejected"):
            raise OrderRefused(400, "invalid_status")
        stmt = stmt.where(CreditTopup.status == payload["status"])
    if payload.get("method") not in (None, ""):
        if payload.get("method") not in ("bank_deposit", "cashier"):
            raise OrderRefused(400, "invalid_method")
        stmt = stmt.where(CreditTopup.method == payload["method"])
    if payload.get("courier_id") not in (None, ""):
        courier_id = _uuid(payload.get("courier_id"))
        if courier_id is None:
            return {"success": True, "topups": []}
        stmt = stmt.where(CreditTopup.courier_id == courier_id)
    topups = []
    for topup, name, phone in (await session.execute(stmt)).all():
        topups.append(
            {
                **topup_view(topup),
                "courier_name": name,
                "courier_phone": phone,
                "cashier_user_id": str(topup.cashier_user_id) if topup.cashier_user_id else None,
                "receipt_url": s3.presign_get(topup.receipt_key, SIGNED_SECONDS) if topup.receipt_key else None,
                "url_expires_in": SIGNED_SECONDS if topup.receipt_key else None,
            }
        )
    return {"success": True, "topups": topups}


async def _cashier(session: AsyncSession, user: CurrentUser) -> CreditCashier:
    row = await session.get(CreditCashier, user.id)
    if row is None or not row.active:
        raise OrderRefused(403, "not_a_cashier")
    return row


def _masked(courier: Courier) -> dict[str, Any]:
    parts = (courier.display_name or "").split()
    name = parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 and parts[-1] else "") if parts else "—"
    phone = courier.phone_e164 or ""
    return {"name": name, "phone_end": phone[-2:] if phone else None, "verified": courier.verification == "verified"}


async def _courier_by_phone(session: AsyncSession, raw: Any) -> Courier:
    phone = to_e164(raw if isinstance(raw, str) else None)
    if phone is None:
        raise OrderRefused(400, "invalid_phone")
    courier = (
        await session.execute(select(Courier).where(Courier.phone_e164 == phone).with_for_update())
    ).scalar_one_or_none()
    if courier is None:
        raise OrderRefused(404, "courier_not_found")
    return courier


async def cashier_lookup(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """lookupCourierForTopup: the cashier checks who he is about to credit (first name + initial)."""
    await _cashier(session, user)
    courier = await _courier_by_phone(session, payload.get("phone"))
    return {"success": True, "courier": _masked(courier)}


async def cashier_topup(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """cashierCreditTopup: cash received at the counter -> credit at once (amount + bonus)."""
    cashier = await _cashier(session, user)
    amount = _amount(payload.get("amount"), low=TOPUP_MIN, high=TOPUP_MAX, error="invalid_amount", whole=True)
    courier = await _courier_by_phone(session, payload.get("phone"))
    if courier.user_id == user.id:
        raise OrderRefused(403, "own_credit")
    bonus = bonus_for(amount)
    now = ot.now_utc()
    topup = CreditTopup(
        courier_id=courier.id,
        method="cashier",
        status="approved",
        amount=amount,
        bonus=bonus,
        cashier_user_id=cashier.user_id,
        reviewed_by=user.id,
        reviewed_at=now,
    )
    session.add(topup)
    await session.flush()
    await _credit(session, courier.id, amount, bonus, user.id)
    await _tell_courier(session, courier, topup)
    return {
        "success": True,
        "topup": topup_view(topup),
        "courier": _masked(courier),
        "credited": _dt(amount + bonus),
        "cash_to_collect": _dt(amount),
    }


async def cashier_today(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """listMyCashierTopups: the cashier's own sales not handed over to ODS yet (his cash drawer)."""
    cashier = await _cashier(session, user)
    rows = list(
        (
            await session.execute(
                select(CreditTopup, Courier)
                .join(Courier, Courier.id == CreditTopup.courier_id)
                .where(
                    CreditTopup.cashier_user_id == cashier.user_id,
                    CreditTopup.method == "cashier",
                    CreditTopup.status == "approved",
                    CreditTopup.remitted_at.is_(None),
                )
                .order_by(CreditTopup.created_at.desc())
                .limit(ADMIN_LIST_MAX)
            )
        ).all()
    )
    total = sum((t.amount for t, _ in rows), ZERO)
    return {
        "success": True,
        "cashier": {"label": cashier.label, "address": cashier.address},
        "to_hand_over": _dt(total),
        "topups": [{**topup_view(t), "courier": _masked(c)} for t, c in rows],
    }


async def set_cashier(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """setCreditCashier (admin): makes a user (by e-mail) a cashier, renames or deactivates him."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    email = payload.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise OrderRefused(400, "invalid_email")
    target = (
        await session.execute(
            select(User).where(func.lower(User.email) == email.strip().lower(), User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target is None:
        raise OrderRefused(404, "user_not_found")
    label = _note(payload.get("label"), 80)
    row = await session.get(CreditCashier, target.id)
    if row is None:
        if not label:
            raise OrderRefused(400, "invalid_label")
        row = CreditCashier(user_id=target.id, label=label, created_by=admin.id)
        session.add(row)
    elif label:
        row.label = label
    if "address" in payload:
        row.address = _note(payload.get("address"), 200)
    if "active" in payload:
        if not isinstance(payload.get("active"), bool):
            raise OrderRefused(400, "invalid_active")
        row.active = payload["active"]
    await session.flush()
    return {"success": True, "cashier": await _cashier_admin_view(session, row)}


async def _cashier_admin_view(session: AsyncSession, row: CreditCashier) -> dict[str, Any]:
    email = (await session.execute(select(User.email).where(User.id == row.user_id))).scalar_one_or_none()
    unremitted = (
        await session.execute(
            select(func.coalesce(func.sum(CreditTopup.amount), 0), func.count()).where(
                CreditTopup.cashier_user_id == row.user_id,
                CreditTopup.method == "cashier",
                CreditTopup.status == "approved",
                CreditTopup.remitted_at.is_(None),
            )
        )
    ).one()
    return {
        "user_id": str(row.user_id),
        "email": email,
        "label": row.label,
        "address": row.address,
        "active": row.active,
        "to_hand_over": _dt(Decimal(unremitted[0])),
        "sales_not_handed_over": unremitted[1],
    }


async def list_cashiers(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    rows = (await session.execute(select(CreditCashier).order_by(CreditCashier.created_at))).scalars()
    return {"success": True, "cashiers": [await _cashier_admin_view(session, r) for r in rows]}


async def mark_remitted(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """markCashierRemitted (admin): the cashier handed over his cash; every open sale is marked."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    cashier_id = _uuid(payload.get("cashier_user_id"))
    if cashier_id is None or await session.get(CreditCashier, cashier_id) is None:
        raise OrderRefused(404, "cashier_not_found")
    result = await session.execute(
        update(CreditTopup)
        .where(
            CreditTopup.cashier_user_id == cashier_id,
            CreditTopup.method == "cashier",
            CreditTopup.status == "approved",
            CreditTopup.remitted_at.is_(None),
        )
        .values(remitted_at=ot.now_utc())
        .returning(CreditTopup.amount)
    )
    amounts = [r[0] for r in result.all()]
    return {"success": True, "sales": len(amounts), "amount": _dt(sum(amounts, ZERO))}


async def grant(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """grantCourierCredit (admin): a prime paid in credit (kind `prime`, max PRIME_MAX) or a
    correction (kind `adjustment`, signed: + adds credit, - removes it). Always with a reason."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    courier_id = _uuid(payload.get("courier_id"))
    courier = await session.get(Courier, courier_id) if courier_id else None
    if courier is None:
        raise OrderRefused(404, "courier_not_found")
    reason = _note(payload.get("reason"))
    if not reason:
        raise OrderRefused(400, "invalid_reason")
    kind = payload.get("kind")
    if kind == "prime":
        amount = _amount(payload.get("amount"), low=Decimal("0.5"), high=PRIME_MAX, error="invalid_amount")
        entry = CourierLedgerEntry(courier_id=courier.id, kind="credit_prime", amount=-amount, created_by=admin.id)
    elif kind == "adjustment":
        amount = _amount(payload.get("amount"), low=-ADJUST_MAX, high=ADJUST_MAX, error="invalid_amount")
        if amount == 0:
            raise OrderRefused(400, "invalid_amount")
        entry = CourierLedgerEntry(courier_id=courier.id, kind="adjustment", amount=-amount, created_by=admin.id)
    else:
        raise OrderRefused(400, "invalid_kind", kinds=["prime", "adjustment"])
    session.add(entry)
    await session.flush()
    credit = await balance(session, courier.id)
    if kind == "prime":
        await notify(
            session,
            user_id=courier.user_id,
            type_="credit_topup_approved",
            title_fr="🎁 Prime ajoutée à votre crédit",
            title_ar="🎁 أضيفت منحة إلى رصيدك",
            body_fr=f"+{_dt(amount)} DT ({reason}). Votre crédit : {_dt(credit)} DT.",
            body_ar=f"+{_dt(amount)} د.ت ({reason}). رصيدك: {_dt(credit)} د.ت.",
            metadata={"recipient_role": "courier", "credit": _dt(credit), "reason": reason},
        )
    return {"success": True, "credit": _dt(credit), "entry": _entry_view(entry)}


async def get_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """getCreditSettings (admin): the bank details as saved (to prefill the form)."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    return {"success": True, "settings": await settings(session)}


async def set_settings(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """setCreditSettings (admin): the bank details shown on "Mon crédit" (account holder, bank, RIB)."""
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    row = await session.get(AppSetting, SETTINGS_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    for field in SETTINGS_FIELDS:
        if field in payload:
            value[field] = _note(payload.get(field), 400)
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=value, updated_by=admin.id))
    else:
        row.value, row.updated_by = value, admin.id
    await session.flush()
    return {"success": True, "settings": {field: value.get(field) for field in SETTINGS_FIELDS}}


async def after_commission(session: AsyncSession, entry: CourierLedgerEntry | None) -> None:
    """Called by commission.record_delivery: when a due commission takes the credit under
    LOW_CREDIT_DELIVERIES commissions, the courier is told once (the crossing, not every delivery)."""
    if entry is None or entry.kind != KIND_DUE:
        return
    credit = await balance(session, entry.courier_id)
    threshold = COMMISSION_PER_DELIVERY_TND * LOW_CREDIT_DELIVERIES
    if not (credit < threshold <= credit + entry.amount):
        return
    courier = await session.get(Courier, entry.courier_id)
    if courier is None:
        return
    left = int((max(credit, ZERO) / COMMISSION_PER_DELIVERY_TND).to_integral_value(rounding=ROUND_DOWN))
    await notify(
        session,
        user_id=courier.user_id,
        type_="credit_low",
        title_fr="⚠️ Crédit presque vide",
        title_ar="⚠️ الرصيد قارب على النفاد",
        body_fr=(
            f"Crédit : {_dt(credit)} DT, encore {left} livraison(s). Rechargez pour garder les commandes "
            "de vos clients."
        ),
        body_ar=f"الرصيد: {_dt(credit)} د.ت، تبقى {left} توصيلة. اشحن رصيدك لتحافظ على طلبات حرفائك.",
        metadata={"recipient_role": "courier", "credit": _dt(credit), "deliveries_left": left},
    )
