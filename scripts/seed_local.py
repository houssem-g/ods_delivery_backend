"""Local seed (idempotent): admin, the Playwright QA accounts, default app settings.

    make seed        (= uv run python -m scripts.seed_local)

QA e-mails / passwords come from the environment (TEST_DUAL_EMAIL, TEST_DUAL_PASSWORD,
TEST_COURIER_EMAIL, TEST_COURIER_PASSWORD, TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD) or, when
unset, from the Playwright constants file (QA_CONSTANTS_PATH, default
../ods-delivery/tests/helpers/constants.ts), read at run time and never printed.
Re-seeding also clears what the live Playwright suites leave on the QA accounts and would
otherwise block the next run: no-response incidents of their orders (5 = customer suspended,
placeOrder 403 customer_suspended) and the courier's late-cancellation counter.
The local admin is LOCAL_ADMIN_EMAIL / LOCAL_ADMIN_PASSWORD; without a password it is
created passwordless and signs in through the account-setup flow (code in Mailpit).
Refuses to run outside ENVIRONMENT=local/test.
"""

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal, engine
from app.models import AppSetting, Courier, NoResponseCase, Order, User, UserAddress
from app.security.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONSTANTS = ROOT.parent / "ods-delivery" / "tests" / "helpers" / "constants.ts"

SOUSSE = (35.8256, 10.6084)
SAHLOUL = (35.8364, 10.5937)


@dataclass(frozen=True)
class Account:
    email: str
    password: str | None


def _constants() -> dict[str, str]:
    """NAME -> default value of `export const NAME = process.env.X ?? '<value>'` lines."""
    path = Path(os.environ.get("QA_CONSTANTS_PATH") or DEFAULT_CONSTANTS)
    if not path.is_file():
        return {}
    pattern = re.compile(r"export const (\w+)\s*=\s*process\.env\.\w+\s*\?\?\s*'([^']*)'")
    return dict(pattern.findall(path.read_text(encoding="utf-8")))


def qa_account(prefix: str, constants: dict[str, str]) -> Account | None:
    email = os.environ.get(f"TEST_{prefix}_EMAIL") or constants.get(f"{prefix}_EMAIL")
    password = os.environ.get(f"TEST_{prefix}_PASSWORD") or constants.get(f"{prefix}_PASSWORD")
    return Account(email, password) if email else None


def _point(lat_lng: tuple[float, float]) -> str:
    return f"SRID=4326;POINT({lat_lng[1]} {lat_lng[0]})"


async def upsert_user(session, account: Account, full_name: str, role: str, profile: bool) -> User:
    user = (await session.execute(select(User).where(User.email == account.email))).scalar_one_or_none()
    if user is None:
        user = User(email=account.email, full_name=full_name)
        session.add(user)
    user.full_name = user.full_name or full_name
    if role == "admin" or user.role != "admin":
        user.role = role
    user.password_hash = hash_password(account.password) if account.password else user.password_hash
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    user.disabled_at = None
    if profile:
        user.profile_created_at = user.profile_created_at or datetime.now(UTC)
    await session.flush()
    return user


async def ensure_address(session, user: User, where: tuple[float, float], street: str) -> None:
    row = (
        await session.execute(
            select(UserAddress).where(UserAddress.user_id == user.id, UserAddress.is_default)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            UserAddress(
                user_id=user.id, is_default=True, address=street, governorate="Sousse", city="Sousse",
                country="TN", location=_point(where),
            )
        )  # fmt: skip


async def ensure_courier(session, user: User, verified_by: User | None, **fields) -> Courier:
    courier = (await session.execute(select(Courier).where(Courier.user_id == user.id))).scalar_one_or_none()
    if courier is None:
        courier = Courier(user_id=user.id, **fields)
        session.add(courier)
    else:
        for name, value in fields.items():
            setattr(courier, name, value)
    if courier.verification == "verified":
        courier.verified_at = courier.verified_at or datetime.now(UTC)
        courier.verified_by = verified_by.id if verified_by else None
    await session.flush()
    return courier


async def reset_qa_penalties(session, users: list[User]) -> str:
    """Test runs only: void the QA customers' counted incidents, lift the suspension flag, zero
    the QA couriers' late cancellations (real accounts are never touched)."""
    ids = [u.id for u in users]
    voided = (
        await session.execute(
            update(NoResponseCase)
            .where(
                NoResponseCase.incident_counted.is_(True),
                NoResponseCase.order_id.in_(select(Order.id).where(Order.customer_id.in_(ids))),
            )
            .values(incident_counted=False)
        )
    ).rowcount
    await session.execute(update(User).where(User.id.in_(ids)).values(is_blacklisted=False))
    await session.execute(update(Courier).where(Courier.user_id.in_(ids)).values(late_cancellations=0))
    return f"QA penalties reset ({voided} incident(s) voided)"


async def seed() -> list[str]:
    constants = _constants()
    report: list[str] = []
    async with SessionLocal() as session, session.begin():
        admin_account = Account(
            os.environ.get("LOCAL_ADMIN_EMAIL") or "admin@ods.local",
            os.environ.get("LOCAL_ADMIN_PASSWORD") or None,
        )
        admin = await upsert_user(session, admin_account, "ODS Admin", "admin", profile=True)
        report.append(
            f"admin {admin.email}" + ("" if admin.password_hash else " (no password: use account-setup)")
        )

        test_admin = qa_account("ADMIN", constants)
        if test_admin:
            await upsert_user(session, test_admin, "QA Admin", "admin", profile=True)
            report.append(f"QA admin {test_admin.email}")

        dual = qa_account("DUAL", constants)
        if dual:
            user = await upsert_user(session, dual, "QA Client Livreur", "customer", profile=True)
            user.phone_e164 = user.phone_e164 or "+21622345678"
            user.language = "fr"
            await ensure_address(session, user, SOUSSE, "Avenue Habib Bourguiba, Sousse")
            await ensure_courier(
                session, user, None, display_name="QA Client Livreur", phone_e164="+21698123456",
                id_document_number="09876543", vehicle="scooter", price_per_km=Decimal("0.800"),
                min_fee=Decimal("3.000"), notification_radius_km=Decimal("10"), service_governorate="Sousse",
                service_city="Sousse", verification="pending", is_online=False, last_location=_point(SOUSSE),
            )  # fmt: skip
            report.append(f"QA customer + pending courier {dual.email}")

        courier_account = qa_account("COURIER", constants)
        if courier_account:
            user = await upsert_user(session, courier_account, "QA Livreur", "courier", profile=False)
            await ensure_courier(
                session, user, admin, display_name="QA Livreur", phone_e164="+21655123456",
                id_document_number="07654321", vehicle="scooter", price_per_km=Decimal("1.000"),
                min_fee=Decimal("3.000"), notification_radius_km=Decimal("15"), service_governorate="Sousse",
                service_city="Sahloul", verification="verified", referral_code="QAVOV", is_online=False,
                last_location=_point(SAHLOUL),
            )  # fmt: skip
            report.append(f"QA verified courier {courier_account.email}")

        qa_users = [
            u
            for u in (
                await session.execute(
                    select(User).where(
                        User.email.in_([a.email for a in (dual, courier_account) if a is not None])
                    )
                )
            ).scalars()
        ]
        if qa_users:
            report.append(await reset_qa_penalties(session, qa_users))

        if await session.get(AppSetting, "main") is None:
            session.add(AppSetting(key="main", value={"support_phone": "", "support_whatsapp": ""}))
        report.append("app_settings main")
    return report


async def main() -> int:
    if settings.ENVIRONMENT not in ("local", "test"):
        print(f"refusing to seed with ENVIRONMENT={settings.ENVIRONMENT}")
        return 1
    try:
        for line in await seed():
            print(f"seeded: {line}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
