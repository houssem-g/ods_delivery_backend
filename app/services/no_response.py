"""'Client ne répond pas': the courier bought the goods with his own cash and can't reach the
customer at the door. Port of base44/functions/triggerEmergencyContact (+ its 2026-09-28 fixes).

The server owns the procedure; the screens only display it:
  report_no_response  courier/admin. Opens a case (deadline = now + WAIT), order →
                      client_no_response, alerts the customer by in-app notification + push +
                      WhatsApp (SMS fallback in app.services.whatsapp). Idempotent: a retry
                      while a case is open changes nothing (no second case, alert or deadline).
  status              courier/customer/admin polls (`check_response` = old name). Enforces the
                      timeout: once the deadline is past the incident is recorded ONCE and both
                      sides are told; asks for the SMS fallback when WhatsApp was not delivered
                      in time; closes orders nobody closed AUTO_CLOSE_HOURS after the deadline.
  customer_confirms   customer/admin. Back to on_the_way, even after the deadline, as long as
                      the courier has not resold or cancelled (then 409 too_late). A late
                      answer voids the incident.
  courier_resume      courier/admin: "I reached the customer myself" (same effect).
  realert             courier/admin, once the deadline passed: a second and last urgent alert
                      (new case, new countdown, reminders again). The first case's incident is
                      voided, the new case carries it; MAX_REPORTS = 2 cases per order.
  sweep               the 5-minute job (app/jobs/incidents.py), never an HTTP action.
  reminders           while a case waits, the customer's phone rings again every
                      REMINDER_EVERY (push only, Android channel `urgent_alarm`): one alert is
                      easy to miss with the app closed. Sent by whoever looks first (the
                      courier's status poll, the 15-second `no_response_fast` job).

Rules kept: at most MAX_REPORTS cases per order; reportable only once the goods are bought
(purchased / on_the_way); QA orders never trigger WhatsApp/SMS; a case adopted from an order
parked before the procedure existed ('legacy', old 2-minute rule) never counts an incident nor
sends the "last chance" alerts, it only expires and is auto-closed; an order cancelled by
another path closes its case without adding an incident ('order_cancelled', or
'cancelled_kept' when the deadline had passed); the alerts bypass the notification
preferences (always pushed).
Concurrency: every action holds the order row lock (`ot.lock_order`) from the read that decides
to the writes; the sweep locks with SKIP LOCKED, so a customer answering at that second wins
and the incident is recorded exactly once.
Incidents are derived (`customer_stats`); `orders.mirror_incidents` mirrors the suspension flag.
The Order's no_response_* fields are derived from the latest case, so every case change also
emits an `Order` update.
"""

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, HotDeal, NoResponseCase, Order, OrderStatusEvent, OrderTracking, User
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import cancellation, whatsapp
from app.services import order_transitions as ot
from app.services.order_notices import notify_and_push
from app.services.orders import courier_of_user, first_stop, is_test_order, mirror_incidents
from app.services.push import PushMessage, send_to_user

log = logging.getLogger("odsd.no_response")

# Same values as src/lib/noResponsePolicy.js (base44/tests/no_response_test.ts compares them).
WAIT_SECONDS = 180
AUTO_CLOSE_HOURS = 3
MAX_REPORTS = 2  # the alert, then one re-alert after the deadline; never a third
SMS_CHECK_AFTER = timedelta(seconds=60)
LEGACY_WAIT = cancellation.LEGACY_WAIT  # old rule: 2 minutes
LEGACY = "legacy"

REPORTABLE = frozenset({"purchased", "on_the_way"})
CONFIRMED = frozenset({"customer_confirmed", "courier_reached"})
SOURCE = "triggerEmergencyContact"
SWEEP_LIMIT = 100
REMINDER_EVERY = timedelta(seconds=45)
MAX_REMINDERS = 3  # at 45, 90 and 135 s; the "last chance" alert follows at the deadline
# A refusal answered with a status >= 400 whose writes must still be committed (the router
# rolls back otherwise): set on session.info by the flows, read by the function modules.
COMMIT_REFUSAL = "odsd_commit_refusal"

Result = tuple[int, dict[str, Any]]


def js_iso(value: datetime | None) -> str | None:
    """`new Date(x).toISOString()`: what the Deno function answered."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def keep_writes(session: AsyncSession) -> None:
    session.info[COMMIT_REFUSAL] = True


def _touch(session: AsyncSession, case: NoResponseCase, created: bool = False) -> None:
    """Realtime: the case, and the order whose no_response_* fields derive from it."""
    emit(session, "NoResponseCase", "create" if created else "update", case.id)
    emit(session, "Order", "update", case.order_id)


# ─────────────────────────── case helpers ───────────────────────────


async def latest_case(session: AsyncSession, order_id: uuid.UUID) -> NoResponseCase | None:
    return (
        await session.execute(
            select(NoResponseCase)
            .where(NoResponseCase.order_id == order_id)
            .order_by(NoResponseCase.created_at.desc(), NoResponseCase.id.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _cases(session: AsyncSession, order_id: uuid.UUID) -> list[NoResponseCase]:
    return list(
        (
            await session.execute(
                select(NoResponseCase)
                .where(NoResponseCase.order_id == order_id)
                .order_by(NoResponseCase.created_at.desc(), NoResponseCase.id.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _courier_user(session: AsyncSession, order: Order) -> uuid.UUID | None:
    if order.courier_id is None:
        return None
    return (
        await session.execute(select(Courier.user_id).where(Courier.id == order.courier_id))
    ).scalar_one_or_none()


async def adopt_legacy(session: AsyncSession, order: Order) -> NoResponseCase | None:
    """An order parked in client_no_response without a case (reported before the procedure
    existed) gets one: started when the order entered client_no_response, old 2-minute rule."""
    if order.status != "client_no_response":
        return None
    entered = (
        await session.execute(
            select(OrderStatusEvent.created_at)
            .where(OrderStatusEvent.order_id == order.id, OrderStatusEvent.to_status == "client_no_response")
            .order_by(OrderStatusEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    started = entered or ot.now_utc()
    case = NoResponseCase(
        order_id=order.id,
        courier_id=order.courier_id,
        status="waiting",
        purchase_amount=order.purchase_amount or 0,
        started_at=started,
        deadline_at=started + LEGACY_WAIT,
        incident_counted=False,
        messaging_status=LEGACY,
        channels={},
    )
    session.add(case)
    await session.flush()
    _touch(session, case, created=True)
    return case


async def _current_case(session: AsyncSession, order: Order) -> NoResponseCase | None:
    return await latest_case(session, order.id) or await adopt_legacy(session, order)


def build_view(
    order: Order, case: NoResponseCase | None, now: datetime, reports: int | None = None
) -> dict[str, Any]:
    deadline = case.deadline_at if case is not None else None
    resolved = case is not None and case.status == "resolved"
    expired = (
        case is not None
        and not resolved
        and (case.status == "expired" or (deadline is not None and now >= deadline))
    )
    stage = "none" if case is None else "resolved" if resolved else "expired" if expired else "waiting"
    channels = (case.channels if case is not None else None) or {}
    still_open = order.status == "client_no_response"
    seconds_left = 0
    if stage == "waiting" and deadline is not None:
        seconds_left = max(0, math.ceil((deadline - now).total_seconds()))
    return {
        "stage": stage,
        "case_id": str(case.id) if case is not None else None,
        "started_at": js_iso(case.started_at) if case is not None else None,
        "deadline_at": js_iso(deadline),
        "server_now": js_iso(now),
        "seconds_left": seconds_left,
        "customer_responded": case is not None and case.resolution in CONFIRMED,
        "resolution": case.resolution if case is not None else None,
        "order_status": order.status or "unknown",
        "channels": {
            "in_app": case is not None,
            "push_devices": int(channels.get("push_devices") or 0),
            "whatsapp": channels.get("whatsapp"),
            "sms": channels.get("sms"),
        },
        "can_resell": stage == "expired" and still_open and (order.purchase_amount or 0) > 0,
        "can_cancel_without_penalty": stage == "expired" and still_open,
        "can_realert": (
            stage == "expired"
            and still_open
            and case is not None
            and case.messaging_status != LEGACY
            and reports is not None
            and reports < MAX_REPORTS
        ),
        "reports": reports,
        "max_reports": MAX_REPORTS,
    }


async def _refresh_channels(session: AsyncSession, case: NoResponseCase) -> None:
    if not str(case.messaging_status or "").startswith("whatsapp"):
        return
    try:
        _status, found = await whatsapp.summary(session, f"noresp:{case.id}")
    except Exception:
        log.exception("no-response: channel summary failed for case %s", case.id)
        return
    if not found.get("found"):
        return
    channels = dict(case.channels or {})
    if channels.get("whatsapp") != found.get("whatsapp") or channels.get("sms") != found.get("sms"):
        channels["whatsapp"], channels["sms"] = found.get("whatsapp"), found.get("sms")
        case.channels = channels
        _touch(session, case)


async def _close(
    session: AsyncSession,
    case: NoResponseCase,
    resolution: str,
    now: datetime,
    *,
    counted: bool | None = None,
    late: bool | None = None,
) -> None:
    case.status, case.resolution, case.resolved_at = "resolved", resolution, now
    if counted is not None:
        case.incident_counted = counted
    if late is not None:
        case.customer_answered_late = late
    await session.flush()
    _touch(session, case)


async def _finalize(session: AsyncSession, order: Order, case: NoResponseCase, now: datetime) -> None:
    """The deadline passed without an answer: incident recorded once, both sides told once."""
    case.status, case.final_at, case.incident_counted = "expired", now, True
    await session.flush()
    _touch(session, case)
    incidents = await mirror_incidents(session, order.customer_id)
    mins = round(WAIT_SECONDS / 60)
    courier_user = await _courier_user(session, order)
    if courier_user is not None:
        await notify_and_push(
            session,
            user_id=courier_user,
            order_id=order.id,
            type_="customer_no_response_final",
            title_ar="❌ العميل لم يرد",
            title_fr="❌ Le client ne répond toujours pas",
            body_ar=(
                f"لم يرد العميل خلال {mins} دقائق رغم الإشعار والتنبيه. يمكنك الآن إعادة البيع "
                "(عرض ساخن) أو إرجاع البضاعة للمتجر أو الإلغاء بدون عقوبة."
            ),
            body_fr=(
                f"Aucune réponse en {mins} min malgré la notification et l'alarme. Vous pouvez "
                "maintenant revendre (Offre Chaude), rendre la marchandise au magasin ou annuler, "
                "sans pénalité."
            ),
            metadata={
                "customer_unreachable": True,
                "can_cancel": True,
                "can_resell": True,
                "recipient_role": "courier",
                "case_id": str(case.id),
            },
        )
    await notify_and_push(
        session,
        user_id=order.customer_id,
        order_id=order.id,
        type_="emergency_contact",
        title_ar="⚠️ آخر فرصة: المندوب لا يزال ينتظر",
        title_fr="⚠️ Dernière chance : le livreur attend toujours",
        body_ar=(
            "لم تردّ على المندوب. سُجّلت حادثة عدم رد، ويمكنه الآن إعادة بيع طلبك. "
            "اتصل به فوراً إن كنت لا تزال تريده."
        ),
        body_fr=(
            "Vous n'avez pas répondu au livreur : un incident de non-réponse est enregistré et il "
            "peut maintenant revendre votre commande. Appelez-le tout de suite si vous la voulez "
            "encore."
        ),
        metadata={
            "is_emergency": True,
            "stage": "final",
            "incidents": incidents,
            "recipient_role": "customer",
            "case_id": str(case.id),
        },
    )


async def _auto_close(session: AsyncSession, order: Order, case: NoResponseCase, now: datetime) -> None:
    """Nobody closed the order long after the deadline (courier gone): closed by the system,
    the courier keeps the goods, no penalty; an incident counted by the former code on a
    legacy case is withdrawn."""
    order.cancelled_by, order.cancel_reason = "system", "client_no_response"
    await ot.transition(
        session, order, "cancelled", None, SOURCE, "client_no_response", cancelled_by="system"
    )
    void_legacy = case.messaging_status == LEGACY and case.incident_counted
    await _close(session, case, "auto_closed", now, counted=False if void_legacy else None)
    if void_legacy:
        await mirror_incidents(session, order.customer_id)
    courier_user = await _courier_user(session, order)
    if courier_user is not None:
        await notify_and_push(
            session,
            user_id=courier_user,
            order_id=order.id,
            type_="customer_no_response_final",
            title_ar="تم إغلاق الطلب",
            title_fr="Commande clôturée",
            body_ar="أُغلق الطلب تلقائياً لأن العميل لم يرد. البضاعة تبقى لك، بدون عقوبة.",
            body_fr=(
                "La commande a été clôturée automatiquement : le client n’a jamais répondu. "
                "La marchandise reste à vous, sans pénalité."
            ),
            metadata={"recipient_role": "courier", "case_id": str(case.id), "auto_closed": True},
        )
    await notify_and_push(
        session,
        user_id=order.customer_id,
        order_id=order.id,
        type_="delivery_cancelled",
        title_ar="❌ أُلغي طلبك",
        title_fr="❌ Commande annulée",
        body_ar="أُلغي طلبك لأنك لم ترد على المندوب.",
        body_fr="Votre commande est annulée : vous n'avez pas répondu au livreur.",
        metadata={"recipient_role": "customer", "reason": "client_no_response"},
    )


async def _remind(session: AsyncSession, order: Order, case: NoResponseCase, now: datetime) -> None:
    """Reminder k is due REMINDER_EVERY * k after the report, before the deadline. Push only (no
    in-app row: the bell keeps one alert); at most one per look, missed ones are not replayed."""
    channels = dict(case.channels or {})
    sent = int(channels.get("reminders") or 0)
    due = min(MAX_REMINDERS, int((now - case.started_at) / REMINDER_EVERY))
    if sent >= due or now >= case.deadline_at:
        return
    channels["reminders"] = due
    case.channels = channels
    await session.flush()
    user = await session.get(User, order.customer_id)
    if user is None or user.deleted_at is not None:
        return
    left = max(1, math.ceil((case.deadline_at - now).total_seconds() / 60))
    await send_to_user(
        session,
        order.customer_id,
        PushMessage(
            type="emergency_contact",
            title_ar="🚨 المندوب ينتظرك أمام الباب!",
            title_fr="🚨 Le livreur vous attend devant chez vous !",
            body_ar=f"تذكير {due}/{MAX_REMINDERS}: اتصل بالمندوب أو أكّد توفّرك، بقي {left} د.",
            body_fr=(
                f"Rappel {due}/{MAX_REMINDERS} : appelez-le ou confirmez que vous êtes là. "
                f"Il reste {left} min."
            ),
            order_id=str(order.id),
            metadata={
                "is_emergency": True,
                "priority": "urgent",
                "recipient_role": "customer",
                "stage": "reminder",
                "reminder": due,
                "deadline_at": js_iso(case.deadline_at),
                "case_id": str(case.id),
            },
        ),
    )


async def advance(session: AsyncSession, order: Order, case: NoResponseCase | None) -> NoResponseCase | None:
    """The timeout, enforced by whoever looks first (courier, customer, sweep). Idempotent;
    the caller holds the order lock."""
    if case is None or case.status == "resolved":
        return case
    now = ot.now_utc()

    # The order left client_no_response without the procedure: close the case, blame nobody new.
    if order.status != "client_no_response":
        if order.status == "delivered":
            was_counted = case.incident_counted
            await _close(session, case, "delivered", now, counted=False)
            if was_counted:
                await mirror_incidents(session, order.customer_id)
        elif order.status == "cancelled":
            # keep an incident already counted, never add one here
            await _close(
                session, case, "cancelled_kept" if case.status == "expired" else "order_cancelled", now
            )
        return case

    channels = case.channels or {}
    if (
        now - case.started_at >= SMS_CHECK_AFTER
        and channels.get("whatsapp") == "sent"
        and not channels.get("sms")
    ):
        # WhatsApp not delivered after 60 s → SMS (the WhatsApp service decides).
        try:
            async with session.begin_nested():
                await whatsapp.check_pending(session, order.id)
        except Exception:
            log.exception("no-response: check_pending failed for order %s", order.id)
        await _refresh_channels(session, case)

    legacy = case.messaging_status == LEGACY
    if case.status == "waiting" and not legacy and now < case.deadline_at:
        await _remind(session, order, case, now)
    if case.status == "waiting" and now >= case.deadline_at:
        if legacy:
            # never given a server deadline: no incident, no "last chance" alerts; only expires
            case.status, case.final_at, case.incident_counted = "expired", now, False
            await session.flush()
            _touch(session, case)
        else:
            await _finalize(session, order, case, now)
            return case

    if case.status == "expired" and now >= case.deadline_at + timedelta(hours=AUTO_CLOSE_HOURS):
        await _auto_close(session, order, case, now)
    return case


async def refresh(session: AsyncSession, order: Order) -> None:
    """cancelOrder / createHotDeal: bring the order's case up to date (the live code called
    triggerEmergencyContact {action:'status'}). The order is locked by the caller."""
    await advance(session, order, await _current_case(session, order))


# ─────────────────────────── actions ───────────────────────────


def _label(stop_name: str | None, order: Order) -> str:
    return (stop_name or "").strip() or (order.items_text or "").strip()[:40] or "ODS"


REPORT_MAX_DISTANCE_M = 300
POSITION_MAX_AGE = timedelta(minutes=10)


async def _courier_distance_m(session: AsyncSession, order: Order) -> float | None:
    """Metres between the courier's latest fresh position (live tracking of this order, else his
    last known position) and the delivery address; None without a position of the last 10 min."""
    since = ot.now_utc() - POSITION_MAX_AGE
    tracked = (
        select(OrderTracking.location)
        .where(OrderTracking.order_id == order.id, OrderTracking.recorded_at >= since)
        .order_by(OrderTracking.recorded_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    last = (
        select(Courier.last_location)
        .where(Courier.id == order.courier_id, Courier.last_seen_at >= since)
        .scalar_subquery()
    )
    target = select(Order.delivery_location).where(Order.id == order.id).scalar_subquery()
    value = (await session.execute(select(func.ST_Distance(func.coalesce(tracked, last), target)))).scalar()
    return float(value) if value is not None else None


async def report(session: AsyncSession, order: Order, actor: CurrentUser) -> Result:
    cases = await _cases(session, order.id)
    open_case = next((c for c in cases if c.status != "resolved"), None)
    if open_case is not None and order.status == "client_no_response":
        case = await advance(session, order, open_case)
        return 200, {"success": True, "already_open": True, **build_view(order, case, ot.now_utc())}
    if order.status not in REPORTABLE:
        return 409, {"error": "not_reportable", "status": order.status}
    if len(cases) >= MAX_REPORTS:
        return 409, {"error": "too_many_reports", "max": MAX_REPORTS}
    if not actor.is_admin and order.delivery_location is not None:
        # Only at the door: it records an incident against the customer (owner, 06/10, QA B34).
        distance = await _courier_distance_m(session, order)
        if distance is None:
            return 409, {"error": "position_unknown", "max_m": REPORT_MAX_DISTANCE_M}
        if distance > REPORT_MAX_DISTANCE_M:
            return 409, {"error": "too_far", "distance_m": round(distance), "max_m": REPORT_MAX_DISTANCE_M}
    now = ot.now_utc()
    if open_case is not None:
        # a case left open while the order moved on: closed before the new one opens
        await _close(session, open_case, "customer_confirmed", now, counted=False)
    return await _open_case(session, order, actor, now, attempt=len(cases) + 1)


async def _open_case(
    session: AsyncSession, order: Order, actor: CurrentUser, now: datetime, attempt: int
) -> Result:
    """A new countdown: the case, the order parked, the customer alerted on every channel."""
    deadline = now + timedelta(seconds=WAIT_SECONDS)
    case = NoResponseCase(
        order_id=order.id,
        courier_id=order.courier_id,
        status="waiting",
        purchase_amount=order.purchase_amount or 0,
        started_at=now,
        deadline_at=deadline,
        incident_counted=False,
        channels={"in_app": True, "push_devices": 0, "whatsapp": None, "sms": None},
    )
    session.add(case)
    await session.flush()
    _touch(session, case, created=True)
    if order.status != "client_no_response":
        await ot.transition(session, order, "client_no_response", actor, SOURCE)

    courier = await session.get(Courier, order.courier_id) if order.courier_id else None
    courier_name = courier.display_name if courier is not None else ""
    courier_phone = (courier.phone_e164 if courier is not None else None) or ""
    phone_part = f" ({courier_phone})" if courier_phone else ""
    mins = round(WAIT_SECONDS / 60)
    _row, push_devices = await notify_and_push(
        session,
        user_id=order.customer_id,
        order_id=order.id,
        type_="emergency_contact",
        title_ar=(
            "🚨 عاجل: المندوب أمام منزلك!" if attempt == 1 else "🚨 نداء أخير: المندوب لا يزال أمام منزلك!"
        ),
        title_fr=(
            "🚨 Urgent : le livreur est devant chez vous !"
            if attempt == 1
            else "🚨 Dernier appel : le livreur est toujours devant chez vous !"
        ),
        body_ar=(
            f"المندوب {courier_name} اشترى طلبك بماله ولا يستطيع الوصول إليك. "
            f"اتصل به{phone_part} أو أكّد توفّرك خلال {mins} دقائق."
        ),
        body_fr=(
            f"Votre livreur {courier_name} a avancé l'argent de vos achats et n'arrive pas à vous "
            f"joindre. Appelez-le{phone_part} ou confirmez votre disponibilité sous {mins} min."
        ),
        metadata={
            "is_emergency": True,
            "priority": "urgent",
            "vibrate": True,
            "sound": True,
            "requires_action": True,
            "action_type": "confirm_availability",
            "recipient_role": "customer",
            "stage": "alert",
            "attempt": attempt,
            "deadline_at": js_iso(deadline),
            "case_id": str(case.id),
        },
    )

    # WhatsApp (SMS fallback inside), never for QA orders.
    wa: str | None
    sms: str | None = None
    if is_test_order(order.items_text):
        wa = "skipped_test"
    else:
        stop = await first_stop(session, order.id)
        try:
            async with session.begin_nested():
                _status, answer = await whatsapp.send_template(
                    session,
                    template_key="customer_no_response",
                    params=[
                        courier_name or "ODS",
                        _label(stop.name if stop else None, order),
                        courier_phone or "-",
                    ],
                    idempotency_key=f"noresp:{case.id}",
                    to=order.contact_phone_e164 or None,
                    user_id=order.customer_id,
                    order_id=order.id,
                    critical=True,
                )
            wa = (
                answer.get("whatsapp")
                or answer.get("reason")
                or ("sent" if answer.get("success") else "failed")
            )
            sms = answer.get("sms_status") or answer.get("fallback") or None
        except Exception:
            log.exception("no-response: WhatsApp call failed for order %s", order.id)
            wa = "error"
    case.channels = {"in_app": True, "push_devices": push_devices, "whatsapp": wa, "sms": sms}
    case.messaging_status = f"whatsapp_{wa}" + (f"/sms_{sms}" if sms else "")
    await session.flush()
    return 200, {
        "success": True,
        "message": "Emergency contact initiated",
        "timeout_seconds": WAIT_SECONDS,
        **build_view(order, case, ot.now_utc(), attempt),
    }


async def realert(session: AsyncSession, order: Order, actor: CurrentUser) -> Result:
    """The deadline passed without an answer: the courier asks for a second (last) urgent alert."""
    cases = await _cases(session, order.id)
    case = await advance(session, order, cases[0] if cases else await adopt_legacy(session, order))
    if order.status != "client_no_response" or case is None or case.status != "expired":
        return 409, {"error": "not_expired", "status": order.status}
    if case.messaging_status == LEGACY or len(cases) >= MAX_REPORTS:
        return 409, {"error": "too_many_reports", "max": MAX_REPORTS}
    if await _live_deal(session, order.id) is not None:
        return 409, {"error": "too_late", "reason": "resold", "status": order.status}
    now = ot.now_utc()
    # the incident moves to the new case: counted once, when (if) that one runs out too
    was_counted = case.incident_counted
    await _close(session, case, "realerted", now, counted=False)
    if was_counted:
        await mirror_incidents(session, order.customer_id)
    return await _open_case(session, order, actor, now, attempt=len(cases) + 1)


async def status(session: AsyncSession, order: Order) -> Result:
    case = await advance(session, order, await _current_case(session, order))
    reports = len(await _cases(session, order.id))
    return 200, {"success": True, **build_view(order, case, ot.now_utc(), reports)}


async def _live_deal(session: AsyncSession, order_id: uuid.UUID) -> HotDeal | None:
    return (
        await session.execute(
            select(HotDeal)
            .where(HotDeal.original_order_id == order_id, HotDeal.status.in_(("available", "sold")))
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def answered(session: AsyncSession, order: Order, how: str, actor: CurrentUser) -> Result:
    """The customer (or the courier, by phone) answered: back to delivery unless the goods
    are already gone."""
    case = await _current_case(session, order)

    def too_late(reason: str) -> Result:
        return 409, {
            "error": "too_late",
            "reason": reason,
            "status": order.status,
            "resolution": case.resolution if case is not None else None,
        }

    if order.status in ("cancelled", "delivered"):
        return too_late(order.status)
    now = ot.now_utc()
    if order.status != "client_no_response":
        return 200, {
            "success": True,
            "already": True,
            "message": "Customer confirmed availability",
            **build_view(order, case, now),
        }
    if case is None or case.status == "resolved":
        return 409, {"error": "no_open_case", "status": order.status}

    if await _live_deal(session, order.id) is not None:
        # A deal of these goods exists (the resale wins): the order is closed as resold. With the
        # order lock createHotDeal and this call are serialized, so this is only a safety net for
        # data written outside the procedure.
        order.cancelled_by, order.cancel_reason = "courier", "client_no_response"
        await ot.transition(session, order, "cancelled", None, SOURCE, "resold", cancelled_by="courier")
        case.final_at = case.final_at or now
        await _close(session, case, "resold", now, counted=True)
        await mirror_incidents(session, order.customer_id)
        keep_writes(session)
        return too_late("resold")

    was_counted = case.incident_counted
    late = case.status == "expired" or case.final_at is not None
    await _close(session, case, how, now, counted=False, late=late)
    if was_counted:
        await mirror_incidents(session, order.customer_id)
    await ot.transition(session, order, "on_the_way", actor, how)

    if how == "customer_confirmed":
        courier_user = await _courier_user(session, order)
        if courier_user is not None:
            name = order.contact_name or ""
            await notify_and_push(
                session,
                user_id=courier_user,
                order_id=order.id,
                type_="customer_responded",
                title_ar="✅ العميل رد!",
                title_fr="✅ Le client a répondu !",
                body_ar=f"العميل {name} أكد أنه متاح. اتصل به وأكمل التوصيل.",
                body_fr=(
                    f"Le client {name} a confirmé qu'il est disponible. Appelez-le et terminez la livraison."
                ),
                metadata={
                    "customer_available": True,
                    "action_type": "retry_contact",
                    "recipient_role": "courier",
                },
            )
    return 200, {
        "success": True,
        "message": "Customer confirmed availability",
        **build_view(order, case, ot.now_utc()),
    }


async def route(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    action = str(payload.get("action") or "")
    if action == "sweep":
        return 403, {"error": "Forbidden"}  # the scheduled job only (app/jobs/incidents.py)
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return 400, {"error": "order_id is required"}
    order = await ot.lock_order(session, order_id)
    if order is None:
        return 404, {"error": "Order not found"}
    is_customer = user.is_admin or order.customer_id == user.id
    is_courier = user.is_admin
    if not is_courier and order.courier_id is not None:
        mine = await courier_of_user(session, user.id)
        is_courier = mine is not None and mine.id == order.courier_id

    if action == "report_no_response":
        return await report(session, order, user) if is_courier else (403, {"error": "Forbidden"})
    if action in ("status", "check_response"):
        return await status(session, order) if (is_courier or is_customer) else (403, {"error": "Forbidden"})
    if action == "customer_confirms":
        if not is_customer:
            return 403, {"error": "Forbidden"}
        return await answered(session, order, "customer_confirmed", user)
    if action == "realert_no_response":
        return await realert(session, order, user) if is_courier else (403, {"error": "Forbidden"})
    if action == "courier_resume":
        if not is_courier:
            return 403, {"error": "Forbidden"}
        return await answered(session, order, "courier_reached", user)
    return 400, {"error": "Invalid action"}


# ─────────────────────────── sweep (5-minute job) ───────────────────────────


async def due_order_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Orders parked in client_no_response, plus orders that left it with a case still open
    (closed without the procedure), most recently changed first."""
    open_case = (
        select(NoResponseCase.id)
        .where(NoResponseCase.order_id == Order.id, NoResponseCase.status != "resolved")
        .exists()
    )
    return list(
        (
            await session.execute(
                select(Order.id)
                .where(or_(Order.status == "client_no_response", open_case))
                .order_by(Order.updated_at.desc(), Order.id)
                .limit(SWEEP_LIMIT)
            )
        ).scalars()
    )


async def waiting_order_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Orders whose case is still counting down (or just ran out): the 15-second job sends
    their reminders and the "last chance" alerts on time."""
    return list(
        (
            await session.execute(
                select(NoResponseCase.order_id)
                .where(NoResponseCase.status == "waiting")
                .distinct()
                .limit(SWEEP_LIMIT)
            )
        ).scalars()
    )


async def sweep_order(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """One order of the sweep (its own transaction). SKIP LOCKED: a flow holding the order
    (customer answering, courier reselling) wins; the next run looks again. True when the
    case moved."""
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None:
        return False
    case = await _current_case(session, order)
    before = (case.status, case.resolution) if case is not None else None
    after_case = await advance(session, order, case)
    after = (after_case.status, after_case.resolution) if after_case is not None else None
    return after != before


# cancelOrder calls it before judging a no-response cancellation (app/services/cancellation.py).
cancellation.no_response_refresh = refresh
