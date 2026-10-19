"""Outgoing e-mail: SMTP (Mailpit locally) or `log` (tests: kept in OUTBOX, nothing sent)."""

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import urlencode

import aiosmtplib

from app.config import settings
from app.services.email_templates import CodePurpose, RenderedEmail, code_email

log = logging.getLogger("odsd.email")


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    text: str


# Messages "sent" by the log provider, newest last (tests read the codes from here).
OUTBOX: list[SentEmail] = []


async def send_email(to: str, rendered: RenderedEmail) -> None:
    if settings.EMAIL_PROVIDER == "log":
        OUTBOX.append(SentEmail(to=to, subject=rendered.subject, text=rendered.text))
        log.info("email (log provider) to=%s subject=%s", to, rendered.subject)
        return
    message = EmailMessage()
    message["From"] = settings.EMAIL_FROM
    message["To"] = to
    message["Subject"] = rendered.subject
    message.set_content(rendered.text)
    message.add_alternative(rendered.html, subtype="html")
    await aiosmtplib.send(
        message,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USERNAME or None,
        password=settings.SMTP_PASSWORD or None,
        start_tls=settings.SMTP_STARTTLS,
        use_tls=settings.SMTP_TLS,
        timeout=settings.SMTP_TIMEOUT_SECONDS,
    )


async def send_code_email(
    to: str, purpose: CodePurpose, code: str, language: str, link_token: str | None = None
) -> None:
    """Never raises: the user can ask for a new code; the failure is logged."""
    link = None
    if link_token:
        link = f"{settings.PUBLIC_APP_URL.rstrip('/')}/Welcome?{urlencode({'reset_token': link_token})}"
    from app.services import email_reachability

    try:
        await send_email(to, code_email(purpose, code, language, settings.EMAIL_CODE_TTL_MINUTES, link))
    except Exception as exc:
        refusal = recipient_refusal(exc)
        if refusal is None:
            log.exception("sending the %s code e-mail failed", purpose)
            return
        # The server refused the address itself: the code screen says « vérifiez votre adresse ».
        log.warning("the mail server refused the address of a %s code: %s", purpose, refusal)
        await email_reachability.mark_unreachable(to, refusal)
        return
    await email_reachability.mark_reachable(to)


def recipient_refusal(exc: BaseException) -> str | None:
    """« 550 5.1.1 … » when the SMTP server refused the recipient for good (RCPT TO 5xx), else None:
    a 4xx, a refused sender (throttling), the connection… are our side, never the person's address."""
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        refused = list(exc.recipients)
    elif isinstance(exc, aiosmtplib.SMTPRecipientRefused):
        refused = [exc]
    else:
        return None
    for item in refused:
        if 500 <= int(getattr(item, "code", 0) or 0) < 600:
            return f"{item.code} {item.message}".strip()
    return None
