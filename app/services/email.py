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
    try:
        await send_email(to, code_email(purpose, code, language, settings.EMAIL_CODE_TTL_MINUTES, link))
    except Exception:
        log.exception("sending the %s code e-mail failed", purpose)
