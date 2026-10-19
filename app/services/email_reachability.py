"""Can this address receive our e-mail? (owner 19/10/2026: « s'il entre un faux email et qu'on n'arrive
pas à joindre l'email, on doit pouvoir lui dire de vérifier qu'il a bien entré son email »)

Checked at sign-up and when a code is sent again, before any e-mail leaves:

1. the shape (stricter than the register pattern: one @, labels, a real top-level domain);
2. a frequent typo of a common provider (« gmial.com » → « gmail.com »): a suggestion only, the person
   may keep the address;
3. the domain exists and accepts mail: MX records (a « null MX » `0 .`, RFC 7505, refuses mail), or an
   A/AAAA record without MX (the implicit MX of RFC 5321 §5.1). Short timeout, cached per domain; a DNS
   that does not answer NEVER blocks (we only refuse what the DNS clearly says).

Then the SMTP server may still refuse the recipient right away (RCPT TO 5xx): `mark_unreachable` keeps it
on the account (users.email_unreachable_at) and the code screen shows it (`/api/auth/email-status`).

Never blocked: the QA accounts (settings.QA_ACCOUNTS and qa-*@example.test, whose domain has no MX), the
special-use domains (.test, .local …) on a local / test stack, and the addresses already registered (the
callers skip the check for them).
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import update

from app.config import settings
from app.db import transaction
from app.models import User
from app.security.tokens import now_utc

log = logging.getLogger("odsd.email")

Reason = Literal["syntax", "no_mail", "smtp_refused"]

# The sentences of the screen live in the app (FR / AR); these are for the logs and API clients.
MESSAGE_UNREACHABLE = "We cannot send e-mail to this address. Please check that it is written correctly."
MESSAGE_TYPO = "Did you mean another address?"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: Reason | None = None
    # The same address with the provider's domain corrected (« ali@gmial.com » → « ali@gmail.com »).
    suggestion: str | None = None

    def body(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "suggestion": self.suggestion}


# --- 1. shape ------------------------------------------------------------------------------------

_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59})$")


def split(email: str) -> tuple[str, str] | None:
    """(local part, ASCII lower-case domain) of a well-formed address, else None."""
    email = (email or "").strip()
    if email.count("@") != 1 or len(email) > 254:
        return None
    local, domain = email.split("@")
    if not local or len(local) > 64 or not _LOCAL.match(local):
        return None
    domain = domain.rstrip(".").lower()
    try:
        domain = domain.encode("idna").decode("ascii")  # « café.tn » → « xn--caf-dma.tn »
    except UnicodeError:
        return None
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    if not _TLD.match(labels[-1]):
        return None
    return local, domain


# --- 2. typos of the common providers --------------------------------------------------------------

# Second-level name → the top-level domains it really receives mail on (the first one is suggested).
PROVIDERS: dict[str, tuple[str, ...]] = {
    "gmail": ("com",),
    "googlemail": ("com",),
    "yahoo": ("com", "fr"),
    "hotmail": ("com", "fr"),
    "outlook": ("com", "fr"),
    "live": ("com", "fr"),
    "icloud": ("com",),
    "orange": ("fr",),
    "wanadoo": ("fr",),
    "laposte": ("net",),
    "protonmail": ("com",),
    "topnet": ("tn",),
}
COMMON_DOMAINS = frozenset(f"{name}.{tld}" for name, tlds in PROVIDERS.items() for tld in tlds) | {
    "me.com", "msn.com", "aol.com", "gmx.com", "gmx.fr", "free.fr", "sfr.fr", "proton.me", "yandex.com",
    "mail.ru", "gnet.tn", "planet.tn", "email.com", "mail.com", "ymail.com", "rocketmail.com", "live.be",
}  # fmt: skip
# Typed instead of « com » / « fr » / « net » (keys never typed on purpose for these providers).
_TLD_TYPOS = {
    "com": {"con", "cmo", "comm", "cm", "co", "om", "vom", "xom", "cpm", "coom", "ocm", "c0m", "cim", "clm"},
    "fr": {"fe", "frr", "rf", "ft", "dr", "gr"},
    "net": {"ne", "nt", "nett", "met", "bet"},
    "tn": {"nt", "tnn", "t"},
}
# Names too short to guess from (« live » vs « love ») are only fixed on their top-level domain.
_FUZZY_MIN_LENGTH = 5


def _distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): a swap of two letters counts 1."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


def _provider_for(name: str) -> str | None:
    if name in PROVIDERS:
        return name
    best, best_distance = None, 99
    for provider in PROVIDERS:
        if len(provider) < _FUZZY_MIN_LENGTH:
            continue
        allowed = 1 if len(provider) <= 6 else 2
        d = _distance(name, provider)
        if d <= allowed and d < best_distance:
            best, best_distance = provider, d
    return best


def _fix_tld(provider: str, tld: str) -> str | None:
    tlds = PROVIDERS[provider]
    if tld in tlds:
        return tld
    for good in tlds:
        if tld in _TLD_TYPOS.get(good, ()) or (_distance(tld, good) == 1 and len(tld) >= len(good)):
            return good
    return None


def suggest(email: str) -> str | None:
    """« ali@gmial.com » → « ali@gmail.com »; None when nothing looks mistyped."""
    raw = (email or "").strip()
    if raw.count("@") != 1:
        return None
    local, domain = raw.split("@")
    domain = domain.strip().rstrip(".").lower()
    if not local or not domain or domain in COMMON_DOMAINS:
        return None
    labels = domain.split(".")
    if len(labels) == 1:
        # « ali@gmail » or « ali@gmailcom »: the dot (and the « com ») forgotten
        for provider, tlds in PROVIDERS.items():
            for tld in ("", *tlds):
                if len(provider) >= _FUZZY_MIN_LENGTH and labels[0] == provider + tld:
                    return f"{local}@{provider}.{tld or tlds[0]}"
        return None
    if len(labels) != 2:
        return None
    name, tld = labels
    provider = _provider_for(name)
    if provider is None:
        return None
    fixed_tld = _fix_tld(provider, tld)
    if fixed_tld is None:
        if provider != name:
            fixed_tld = PROVIDERS[provider][0]  # both wrong (« hotmial.cm »): the usual domain
        else:
            return None  # « gmail.de », « outlook.be »: a real domain of the provider, not ours to judge
    candidate = f"{provider}.{fixed_tld}"
    return None if candidate == domain else f"{local}@{candidate}"


# --- 3. does the domain accept mail? ------------------------------------------------------------

DnsLookup = Callable[[str], Awaitable[bool | None]]


async def _dns_accepts_mail(domain: str) -> bool | None:
    """True: MX (not null) or A/AAAA; False: the domain does not exist or has a null MX / no address;
    None: no clear answer (timeout, resolver down…)."""
    import dns.asyncresolver
    import dns.exception
    import dns.name
    import dns.resolver

    timeout = settings.EMAIL_DNS_TIMEOUT_SECONDS
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    # Absolute name, no search list: with « search . » in resolv.conf a relative name made a missing
    # domain wait for the whole timeout instead of answering NXDOMAIN at once.
    fqdn = f"{domain}."
    try:
        answer = await resolver.resolve(fqdn, "MX", search=False)
        return any(record.exchange != dns.name.root for record in answer)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass
    except (dns.exception.DNSException, OSError):
        return None
    for rtype in ("A", "AAAA"):
        try:
            await resolver.resolve(fqdn, rtype, search=False)
            return True
        except dns.resolver.NXDOMAIN:
            return False
        except dns.resolver.NoAnswer:
            continue
        except (dns.exception.DNSException, OSError):
            return None
    return False


_lookup: DnsLookup = _dns_accepts_mail
# domain → (answer, valid until monotonic time); unclear answers are never kept
_cache: dict[str, tuple[bool, float]] = {}
_CACHE_MAX = 5000
POSITIVE_TTL_SECONDS = 6 * 3600
NEGATIVE_TTL_SECONDS = 15 * 60


def set_lookup(lookup: DnsLookup | None) -> None:
    """Tests: a fake DNS (None puts the real one back). The cache is emptied."""
    global _lookup
    _lookup = lookup or _dns_accepts_mail
    _cache.clear()


async def domain_accepts_mail(domain: str) -> bool | None:
    now = time.monotonic()
    hit = _cache.get(domain)
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        answer = await asyncio.wait_for(_lookup(domain), timeout=settings.EMAIL_DNS_TIMEOUT_SECONDS + 1)
    except Exception:  # timeout or a broken resolver: never blocks the sign-up
        log.info("email domain check: no answer for %s", domain)
        return None
    if answer is None:
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[domain] = (answer, now + (POSITIVE_TTL_SECONDS if answer else NEGATIVE_TTL_SECONDS))
    return answer


# --- the verdict ---------------------------------------------------------------------------------

_SPECIAL_USE = (".test", ".example", ".invalid", ".localhost", ".local")


def exempt(email: str) -> bool:
    """QA accounts (no MX on example.test) and the special-use domains of a local / test stack."""
    address = (email or "").strip().lower()
    if address in {e.lower() for e in settings.QA_ACCOUNTS}:
        return True
    local, _, domain = address.rpartition("@")
    if domain == "example.test" and local.startswith("qa-"):
        return True
    return settings.is_local and ("." + domain).endswith(_SPECIAL_USE)


async def check(email: str) -> Verdict:
    if exempt(email):
        return Verdict(ok=True)
    parts = split(email)
    if parts is None:
        return Verdict(ok=False, reason="syntax", suggestion=suggest(email))
    _, domain = parts
    suggestion = suggest(email)
    if not settings.EMAIL_DNS_CHECK or domain in COMMON_DOMAINS:
        return Verdict(ok=True, suggestion=suggestion)
    if await domain_accepts_mail(domain) is False:
        return Verdict(ok=False, reason="no_mail", suggestion=suggestion)
    return Verdict(ok=True, suggestion=suggestion)


# --- the SMTP server refused the recipient ----------------------------------------------------------


async def mark_unreachable(email: str, reason: str) -> None:
    """Kept on the account(s) of this address: the code screen tells the person (email-status)."""
    try:
        async with transaction() as session:
            await session.execute(
                update(User)
                .where(User.email == email.strip(), User.email_verified_at.is_(None))
                .values(email_unreachable_at=now_utc(), email_unreachable_reason=reason[:300])
            )
    except Exception:
        log.exception("could not record the unreachable address")


async def mark_reachable(email: str) -> None:
    """The server took the message: an earlier refusal of this address is forgotten."""
    try:
        async with transaction() as session:
            await session.execute(
                update(User)
                .where(User.email == email.strip(), User.email_unreachable_at.is_not(None))
                .values(email_unreachable_at=None, email_unreachable_reason=None)
            )
    except Exception:
        log.exception("could not clear the unreachable address")
