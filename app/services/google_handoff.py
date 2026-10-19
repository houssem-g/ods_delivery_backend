"""« Continuer avec Google » inside the phone app (owner 19/10/2026).

Google refuses its sign-in page inside an embedded WebView, and the installed apps (Capacitor
shells 1.3.x) have no deep link back. So:

1. the app asks `start` → a handoff id (public, rides in Google's `state`) and a secret (kept by
   the app only), then navigates to Google's page: Capacitor opens it in the phone's browser;
2. Google sends the browser to our callback: the account is linked/created and stored on the
   handoff (`complete`), and the browser shows « revenez dans l'application »;
3. the app, polling `claim` with its secret, receives the session (refresh cookie in the app).

Single use, 10 minutes. The browser that finishes must come from the same network as the app
that started (same IPv4 address, same IPv6 /64): a Google link sent to someone else by message
cannot hand that person's account to the sender's phone.
"""

import hashlib
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuthHandoff
from app.security.tokens import now_utc

TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class Started:
    id: str
    secret: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def same_network(a: str | None, b: str | None) -> bool:
    """Same IPv4 address, or same IPv6 /64 (phones rotate their temporary IPv6 suffix)."""
    if not a or not b:
        return False
    try:
        ia, ib = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return a == b
    if ia.version != ib.version:
        return False
    if ia.version == 6:
        return ipaddress.ip_network(f"{ia}/64", strict=False) == ipaddress.ip_network(
            f"{ib}/64", strict=False
        )
    return ia == ib


async def start(session: AsyncSession, client_ip: str | None) -> Started:
    now = now_utc()
    await session.execute(delete(AuthHandoff).where(AuthHandoff.expires_at < now - TTL))
    handoff_id, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    session.add(
        AuthHandoff(id=handoff_id, secret_hash=_hash(secret), client_ip=client_ip, expires_at=now + TTL)
    )
    await session.flush()
    return Started(id=handoff_id, secret=secret)


async def open_for_callback(
    session: AsyncSession, handoff_id: str, browser_ip: str | None
) -> AuthHandoff | str:
    """The handoff the browser may complete, or the reason it may not."""
    row = await session.get(AuthHandoff, handoff_id, with_for_update=True)
    if row is None or row.expires_at <= now_utc():
        return "handoff_expired"
    if row.completed_at is not None:
        return "handoff_used"
    if not same_network(row.client_ip, browser_ip):
        return "handoff_other_device"
    return row


def complete(row: AuthHandoff, *, user_id: uuid.UUID | None = None, error: str | None = None) -> None:
    row.user_id, row.error, row.completed_at = user_id, error, now_utc()


@dataclass(frozen=True)
class Claim:
    state: str  # "pending" | "done" | "error"
    user_id: uuid.UUID | None = None
    error: str | None = None


async def claim(session: AsyncSession, handoff_id: str, secret: str) -> Claim:
    """Pending while the browser has not finished; done (once: the row goes) with the account."""
    row = await session.get(AuthHandoff, handoff_id, with_for_update=True)
    if row is None or not secrets.compare_digest(row.secret_hash, _hash(secret)):
        return Claim("error", error="handoff_expired")
    if row.completed_at is None:
        if row.expires_at <= now_utc():
            await session.delete(row)
            return Claim("error", error="handoff_expired")
        return Claim("pending")
    await session.delete(row)
    if row.error or row.user_id is None:
        return Claim("error", error=row.error or "google_failed")
    return Claim("done", user_id=row.user_id)
