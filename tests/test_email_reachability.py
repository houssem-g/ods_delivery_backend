"""Sign-up e-mail reachability (owner 19/10/2026): « s'il entre un faux email et qu'on n'arrive pas à
joindre l'email, on doit pouvoir lui dire de vérifier qu'il a bien entré son email ».

The DNS is always simulated (no network): `email_reachability.set_lookup` for the API, a fake
`dns.asyncresolver.Resolver.resolve` for the lookup itself. The SMTP refusal is a patched `send_email`.
"""

import asyncio
from types import SimpleNamespace

import aiosmtplib
import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.services import email as email_service
from app.services import email_reachability as reach
from tests.factories import PASSWORD, error_of
from tests.test_auth import age_codes, last_code


class FakeDns:
    """domain → True / False / None (no answer); unknown domains accept mail. Counts the lookups."""

    def __init__(self, answers: dict[str, bool | None] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    async def __call__(self, domain: str) -> bool | None:
        self.asked.append(domain)
        return self.answers.get(domain, True)


@pytest.fixture
def fake_dns():
    fake = FakeDns({"nowhere-mail.tn": False, "silent-dns.tn": None})
    reach.set_lookup(fake)
    return fake


async def register(client, email: str, **extra):
    body = {"email": email, "password": PASSWORD, "full_name": "Sami Ben Ali", **extra}
    return await client.post("/api/auth/register", json=body)


async def user_row(email: str) -> User | None:
    async with SessionLocal() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()


# --- shape and typos (pure functions) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("ali@gmial.com", "ali@gmail.com"),
        ("ali@gamil.com", "ali@gmail.com"),
        ("ali@gmai.com", "ali@gmail.com"),
        ("ali@gmail.con", "ali@gmail.com"),
        ("ali@gmail.co", "ali@gmail.com"),
        ("ali@gmail", "ali@gmail.com"),
        ("ali@gmailcom", "ali@gmail.com"),
        ("Ali.Ben@GMIAL.COM", "Ali.Ben@gmail.com"),
        ("ali@hotmial.com", "ali@hotmail.com"),
        ("ali@hotmial.fr", "ali@hotmail.fr"),
        ("ali@hotmail.cm", "ali@hotmail.com"),
        ("ali@yaho.fr", "ali@yahoo.fr"),
        ("ali@yahooo.com", "ali@yahoo.com"),
        ("ali@outlok.com", "ali@outlook.com"),
        ("ali@outlook.fr", None),
        ("ali@iclod.com", "ali@icloud.com"),
        ("ali@topnet.nt", "ali@topnet.tn"),
        # real addresses are left alone
        ("ali@gmail.com", None),
        ("ali@hotmail.fr", None),
        ("ali@hotmail.it", None),
        ("ali@email.com", None),
        ("ali@ymail.com", None),
        ("test.admin@ods.tn", None),
        ("lemelec346@sixoplus.com", None),
    ],
)
def test_typo_suggestions(typed, expected):
    assert reach.suggest(typed) == expected


@pytest.mark.parametrize(
    "email",
    ["a..b@x.com", ".a@x.com", "a@b", "a@-x.com", "a@x.c", "a@x.c0m", "é@x.com", "a@@x.com", "a b@x.com"],
)
def test_badly_written_addresses(email):
    assert reach.split(email) is None


def test_well_written_addresses():
    assert reach.split(" Ali.Ben+ods@GMAIL.com ") == ("Ali.Ben+ods", "gmail.com")
    assert reach.split("ali@café.tn") == ("ali", "xn--caf-dma.tn")


def test_exemptions_qa_and_special_use(monkeypatch):
    assert reach.exempt("qa-client9@example.test")  # QA campaign accounts: example.test has no MX
    assert reach.exempt(settings.QA_ACCOUNTS[0].upper())
    assert reach.exempt("someone@ods.local")  # local / test stack only
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert reach.exempt("qa-client9@example.test")
    assert not reach.exempt("someone@ods.local")
    assert not reach.exempt("someone@example.test")


# --- the DNS lookup (resolver simulated) ------------------------------------------------------------


def _mx(*hosts: str):
    return [SimpleNamespace(exchange=dns.name.from_text(h)) for h in hosts]


@pytest.fixture
def resolver(monkeypatch):
    """Answers of the fake resolver: (domain, rtype) → a value to return or an exception to raise."""
    table: dict[tuple[str, str], object] = {}

    async def resolve(self, qname, rdtype="A", *args, **kwargs):
        assert str(qname).endswith(".") and kwargs.get("search") is False  # absolute, no search list
        outcome = table.get((str(qname).rstrip("."), str(rdtype)), dns.resolver.NoAnswer())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)
    return table


async def test_dns_mx_and_fallbacks(resolver):
    resolver[("mail.tn", "MX")] = _mx("mx1.mail.tn.")
    resolver[("nullmx.tn", "MX")] = _mx(".")  # RFC 7505: this domain never receives mail
    resolver[("gone.tn", "MX")] = dns.resolver.NXDOMAIN()
    resolver[("web-only.tn", "A")] = ["1.2.3.4"]  # no MX but an address: implicit MX
    resolver[("v6-only.tn", "AAAA")] = ["::1"]
    resolver[("slow.tn", "MX")] = dns.exception.Timeout()
    resolver[("half.tn", "A")] = dns.exception.Timeout()
    check = reach._dns_accepts_mail
    assert await check("mail.tn") is True
    assert await check("nullmx.tn") is False
    assert await check("gone.tn") is False
    assert await check("web-only.tn") is True
    assert await check("v6-only.tn") is True
    assert await check("empty.tn") is False  # neither MX nor address
    assert await check("slow.tn") is None  # the DNS did not answer: unknown, never a refusal
    assert await check("half.tn") is None


async def test_answers_are_cached_but_not_the_silences(monkeypatch):
    fake = FakeDns({"dead.tn": False, "quiet.tn": None})
    reach.set_lookup(fake)
    for _ in range(3):
        assert await reach.domain_accepts_mail("dead.tn") is False
        assert await reach.domain_accepts_mail("quiet.tn") is None
    assert fake.asked.count("dead.tn") == 1
    assert fake.asked.count("quiet.tn") == 3


async def test_a_hanging_or_broken_dns_never_blocks(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_DNS_TIMEOUT_SECONDS", 0.05)

    async def hangs(domain):
        await asyncio.sleep(5)

    reach.set_lookup(hangs)
    assert await reach.domain_accepts_mail("hang.tn") is None
    assert (await reach.check("ali@hang.tn")).ok

    async def breaks(domain):
        raise RuntimeError("resolver crashed")

    reach.set_lookup(breaks)
    assert (await reach.check("ali@broken.tn")).ok


async def test_common_providers_skip_the_dns(fake_dns):
    assert (await reach.check("ali@gmail.com")).ok
    assert fake_dns.asked == []


# --- sign-up ------------------------------------------------------------------------------------------


async def test_register_refuses_a_domain_without_mail_before_sending(client, fake_dns):
    response = await register(client, "sami@nowhere-mail.tn")
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "email_unreachable" and body["reason"] == "no_mail"
    assert email_service.OUTBOX == [] and await user_row("sami@nowhere-mail.tn") is None


async def test_register_refuses_a_badly_written_address(client, fake_dns):
    response = await register(client, "sami@gmail.c0m")  # passes the shape pattern, not the check
    assert response.status_code == 422
    assert response.json() == {
        "error": "email_unreachable",
        "message": reach.MESSAGE_UNREACHABLE,
        "reason": "syntax",
        "suggestion": "sami@gmail.com",
    }


async def test_register_offers_the_typo_fix_then_keeps_the_address_if_asked(client, fake_dns):
    response = await register(client, "sami@gmial.com")
    assert response.status_code == 422
    assert error_of(response) == "email_typo" and response.json()["suggestion"] == "sami@gmail.com"
    assert email_service.OUTBOX == []
    kept = await register(client, "sami@gmial.com", keep_email=True)  # « Non, mon adresse est correcte »
    assert kept.status_code == 201
    assert email_service.OUTBOX[-1].to == "sami@gmial.com"


async def test_register_goes_on_when_the_dns_is_silent(client, fake_dns):
    response = await register(client, "sami@silent-dns.tn")
    assert response.status_code == 201
    assert last_code("sami@silent-dns.tn")


async def test_register_with_a_good_domain_then_verify(client, fake_dns):
    email = "sami@mail-ok.tn"
    assert (await register(client, email)).status_code == 201
    verified = await client.post("/api/auth/verify-otp", json={"email": email, "code": last_code(email)})
    assert verified.status_code == 200 and verified.json()["user"]["is_verified"] is True


async def test_qa_accounts_are_never_blocked_even_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    reach.set_lookup(FakeDns({"example.test": False}))
    assert (await register(client, "qa-client9@example.test")).status_code == 201
    refused = await register(client, "stranger@example.test")
    assert refused.status_code == 422 and error_of(refused) == "email_unreachable"


async def test_registered_addresses_keep_their_usual_answers(client, factory, fake_dns):
    verified = await factory.user(email="old@nowhere-mail.tn")
    imported = await factory.user(email="imported@nowhere-mail.tn", password=None, verified=False)
    taken = await register(client, verified.email)
    assert taken.status_code == 409 and error_of(taken) == "email_taken"
    setup = await register(client, imported.email)
    assert setup.status_code == 409 and error_of(setup) == "account_setup_required"


async def test_login_never_checks_the_address(client, factory):
    fake = FakeDns({"nowhere-mail.tn": False})
    reach.set_lookup(fake)
    user = await factory.user(email="driver@nowhere-mail.tn")
    response = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200
    unverified = await factory.user(email="late@nowhere-mail.tn", verified=False)
    refused = await client.post("/api/auth/login", json={"email": unverified.email, "password": PASSWORD})
    assert refused.status_code == 403 and error_of(refused) == "email_not_verified"  # as before
    assert fake.asked == []


# --- code resend ---------------------------------------------------------------------------------------


async def test_resend_says_when_the_domain_takes_no_mail(client, factory, fake_dns):
    user = await factory.user(email="late@nowhere-mail.tn", verified=False)
    response = await client.post("/api/auth/resend-otp", json={"email": user.email})
    assert response.status_code == 422 and error_of(response) == "email_unreachable"
    ghost = await client.post("/api/auth/resend-otp", json={"email": "ghost@nowhere-mail.tn"})
    assert ghost.status_code == 422  # domain-only: the same answer with or without an account
    assert email_service.OUTBOX == []
    fine = await factory.user(email="late@mail-ok.tn", verified=False)
    assert (await client.post("/api/auth/resend-otp", json={"email": fine.email})).status_code == 200
    assert last_code(fine.email)


# --- the mail server refuses the address at once ---------------------------------------------------


def _refusing(code: int = 550, message: str = "5.1.10 RESOLVER.ADR.RecipientNotFound"):
    async def send(to, rendered):
        raise aiosmtplib.SMTPRecipientsRefused([aiosmtplib.SMTPRecipientRefused(code, message, to)])

    return send


async def test_smtp_refusal_is_shown_on_the_code_screen_then_cleared(client, monkeypatch, fake_dns):
    email = "sami@mail-ok.tn"
    original = email_service.send_email
    monkeypatch.setattr(email_service, "send_email", _refusing())
    assert (await register(client, email)).status_code == 201
    status = await client.post("/api/auth/email-status", json={"email": email})
    assert status.json() == {"unreachable": True, "reason": "smtp_refused", "suggestion": None}
    row = await user_row(email)
    assert row.email_unreachable_at is not None and "550" in row.email_unreachable_reason

    # the next code goes through: the refusal is forgotten
    monkeypatch.setattr(email_service, "send_email", original)
    await age_codes(email)
    assert (await client.post("/api/auth/resend-otp", json={"email": email})).status_code == 200
    status = await client.post("/api/auth/email-status", json={"email": email})
    assert status.json()["unreachable"] is False
    assert (await user_row(email)).email_unreachable_at is None


async def test_our_side_failures_never_blame_the_address(client, monkeypatch, fake_dns):
    async def throttled(to, rendered):
        raise aiosmtplib.SMTPSenderRefused(554, "5.2.0 Sender throttled", "contact@ods")

    monkeypatch.setattr(email_service, "send_email", throttled)
    assert (await register(client, "a@mail-ok.tn")).status_code == 201
    monkeypatch.setattr(email_service, "send_email", _refusing(450, "4.2.0 try later"))
    assert (await register(client, "b@mail-ok.tn")).status_code == 201

    async def down(to, rendered):
        raise aiosmtplib.SMTPConnectError("connection refused")

    monkeypatch.setattr(email_service, "send_email", down)
    assert (await register(client, "c@mail-ok.tn")).status_code == 201
    for email in ("a@mail-ok.tn", "b@mail-ok.tn", "c@mail-ok.tn"):
        assert (await user_row(email)).email_unreachable_at is None
        status = await client.post("/api/auth/email-status", json={"email": email})
        assert status.json()["unreachable"] is False


async def test_verifying_clears_the_flag(client, fake_dns):
    email = "sami@mail-ok.tn"
    await register(client, email)
    async with SessionLocal() as s:
        row = (await s.execute(select(User).where(User.email == email))).scalar_one()
        row.email_unreachable_at = row.created_at  # a refusal recorded earlier
        row.email_unreachable_reason = "550 test"
        await s.commit()
    assert (await client.post("/api/auth/email-status", json={"email": email})).json()["unreachable"] is True
    verified = await client.post("/api/auth/verify-otp", json={"email": email, "code": last_code(email)})
    assert verified.status_code == 200
    row = await user_row(email)
    assert row.email_unreachable_at is None and row.email_unreachable_reason is None


# --- the screen's endpoints ----------------------------------------------------------------------------


async def test_check_email_endpoint(client, fake_dns):
    ok = await client.post("/api/auth/check-email", json={"email": "sami@mail-ok.tn"})
    assert ok.json() == {"ok": True, "reason": None, "suggestion": None}
    typo = await client.post("/api/auth/check-email", json={"email": "sami@hotmial.com"})
    assert typo.json() == {"ok": True, "reason": None, "suggestion": "sami@hotmail.com"}
    dead = await client.post("/api/auth/check-email", json={"email": "sami@nowhere-mail.tn"})
    assert dead.json() == {"ok": False, "reason": "no_mail", "suggestion": None}
    silent = await client.post("/api/auth/check-email", json={"email": "sami@silent-dns.tn"})
    assert silent.json()["ok"] is True


async def test_email_status_of_a_dead_domain_and_of_a_verified_account(client, factory, fake_dns):
    dead = await client.post("/api/auth/email-status", json={"email": "who@nowhere-mail.tn"})
    assert dead.json()["unreachable"] is True and dead.json()["reason"] == "no_mail"
    user = await factory.user(email="done@mail-ok.tn")
    async with SessionLocal() as s:
        row = await s.get(User, user.id)
        row.email_unreachable_at = row.created_at
        await s.commit()
    status = await client.post("/api/auth/email-status", json={"email": user.email})
    assert status.json()["unreachable"] is False  # verified: the code screen is long gone
