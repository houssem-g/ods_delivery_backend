"""triggerEmergencyContact — "client ne répond pas" (the procedure is app/services/no_response.py).

Body: { order_id, action: 'report_no_response' | 'status' | 'check_response' |
'customer_confirms' | 'courier_resume' | 'realert_no_response' | 'customer_no_show' }. Answers: the
procedure view { success, stage, case_id, started_at, deadline_at, server_now, seconds_left,
customer_responded, resolution, order_status, channels {in_app, push_devices, whatsapp, sms}, can_resell,
can_cancel_without_penalty, can_realert, reports, max_reports, return_deadline_at, no_show
{answered_at, wait_until, wait_seconds, seconds_left, ready, max_distance_m} | null, no_show_recorded }
(+ already_open / already / message / timeout_seconds).
customer_no_show (courier, owner 19/10/2026): the customer answered the last alert but does not come;
after the last wait (no_show.wait_until) and within 300 m of the door → a verified no-response (incident
for the customer, no penalty, Offre Chaude / keep / restaurant return as usual). Refusals (409):
no_show_not_applicable, wait (+ deadline_at, seconds_left), position_unknown / too_far (+ max_m),
too_late.
Errors: 'order_id is required' / 'Invalid action' (400), 'Forbidden' (403, also for the
`sweep` action: the 5-minute job does it), 'Order not found' (404), not_reportable / not_expired /
too_many_reports / no_open_case / too_late (409 — never 400, which NoResponsePanel reads as
"older backend" and falls back to a direct Order.update).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import no_response


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    status, body = await no_response.route(session, user, payload)
    if status >= 400 and session.info.pop(no_response.COMMIT_REFUSAL, False):
        await session.commit()
    return status, body
