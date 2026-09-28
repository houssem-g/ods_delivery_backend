from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import AppSetting, Courier, NoResponseCase, User
from app.security.passwords import verify_password
from scripts import seed_local
from tests.order_helpers import OrderWorld

CONSTANTS = """
export const DUAL_EMAIL    = process.env.TEST_DUAL_EMAIL    ?? 'dual@example.test';
export const DUAL_PASSWORD = process.env.TEST_DUAL_PASSWORD ?? 'dual-secret-1';
export const COURIER_EMAIL    = process.env.TEST_COURIER_EMAIL    ?? 'courier@example.test';
export const COURIER_PASSWORD = process.env.TEST_COURIER_PASSWORD ?? 'courier-secret-1';
"""


async def test_seed_is_idempotent_and_reads_the_constants_file(tmp_path, monkeypatch):
    constants = tmp_path / "constants.ts"
    constants.write_text(CONSTANTS)
    monkeypatch.setenv("QA_CONSTANTS_PATH", str(constants))
    monkeypatch.setenv("LOCAL_ADMIN_EMAIL", "root@example.test")
    monkeypatch.delenv("LOCAL_ADMIN_PASSWORD", raising=False)
    for name in ("DUAL", "COURIER", "ADMIN"):
        monkeypatch.delenv(f"TEST_{name}_EMAIL", raising=False)
        monkeypatch.delenv(f"TEST_{name}_PASSWORD", raising=False)

    first = await seed_local.seed()
    await seed_local.seed()
    assert any("root@example.test" in line and "account-setup" in line for line in first)

    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 3
        dual = (await s.execute(select(User).where(User.email == "dual@example.test"))).scalar_one()
        courier_user = (
            await s.execute(select(User).where(User.email == "courier@example.test"))
        ).scalar_one()
        couriers = {c.user_id: c for c in (await s.execute(select(Courier))).scalars()}
        root = (await s.execute(select(User).where(User.email == "root@example.test"))).scalar_one()
        settings_row = await s.get(AppSetting, "main")
    assert verify_password("dual-secret-1", dual.password_hash) and dual.profile_created_at is not None
    assert couriers[dual.id].verification == "pending"
    assert couriers[courier_user.id].verification == "verified" and courier_user.profile_created_at is None
    assert root.role == "admin" and root.password_hash is None
    assert settings_row.value == {"support_phone": "", "support_whatsapp": ""}


async def test_seed_resets_the_qa_accounts_penalties_only(tmp_path, monkeypatch, factory):
    constants = tmp_path / "constants.ts"
    constants.write_text(CONSTANTS)
    monkeypatch.setenv("QA_CONSTANTS_PATH", str(constants))
    for name in ("DUAL", "COURIER", "ADMIN"):
        monkeypatch.delenv(f"TEST_{name}_EMAIL", raising=False)
        monkeypatch.delenv(f"TEST_{name}_PASSWORD", raising=False)
    await seed_local.seed()
    async with SessionLocal() as s:
        dual = (await s.execute(select(User).where(User.email == "dual@example.test"))).scalar_one()
    stranger = await factory.user()
    orders = OrderWorld(factory)
    qa_order = await orders.order(customer=dual, status="cancelled")
    other_order = await orders.order(customer=stranger, status="cancelled")
    async with SessionLocal() as s, s.begin():
        for order in (qa_order, other_order):
            s.add(
                NoResponseCase(
                    order_id=order.id,
                    status="resolved",
                    started_at=datetime.now(UTC),
                    deadline_at=datetime.now(UTC),
                    incident_counted=True,
                )
            )
        await s.execute(update(User).values(is_blacklisted=True))
        await s.execute(update(Courier).values(late_cancellations=3))

    report = await seed_local.seed()

    assert any("1 incident(s) voided" in line for line in report)
    async with SessionLocal() as s:
        counted = {
            c.order_id: c.incident_counted for c in (await s.execute(select(NoResponseCase))).scalars()
        }
        users = {u.email: u for u in (await s.execute(select(User))).scalars()}
        couriers = {c.user_id: c for c in (await s.execute(select(Courier))).scalars()}
    assert counted == {qa_order.id: False, other_order.id: True}
    assert users["dual@example.test"].is_blacklisted is False
    assert users[stranger.email].is_blacklisted is True
    assert all(c.late_cancellations == 0 for uid, c in couriers.items() if uid == dual.id)
