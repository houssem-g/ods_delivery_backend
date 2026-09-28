import asyncio
import random

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import asyncpg_dsn
from app.jobs.registry import JOBS, Job, job
from app.jobs.scheduler import LeaderScheduler, run_job, try_lock
from tests.factories import auth, error_of


async def wait_for(predicate, seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_advisory_lock_has_a_single_owner():
    key = random.randint(1, 2**31)
    first = await try_lock(asyncpg_dsn(settings), key)
    assert first is not None
    assert await try_lock(asyncpg_dsn(settings), key) is None
    await first.close()  # the session ends: the lock is released
    second = await try_lock(asyncpg_dsn(settings), key)
    assert second is not None
    await second.close()


async def test_one_leader_and_takeover(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_LEADER_RETRY_SECONDS", 0)
    key = random.randint(1, 2**31)
    ticks: list[str] = []

    async def tick():
        ticks.append("tick")
        return {"n": len(ticks)}

    jobs = {
        "tick": Job("tick", tick, IntervalTrigger(seconds=1), "test"),
        "off": Job("off", tick, IntervalTrigger(seconds=1), "disabled", enabled=False),
    }
    leader, follower = LeaderScheduler(jobs, key), LeaderScheduler(jobs, key)
    leader.start()
    await wait_for(lambda: leader.is_leader)
    assert [j.id for j in leader._scheduler.get_jobs()] == ["tick"]
    follower.start()
    await asyncio.sleep(0.3)
    assert not follower.is_leader

    await leader.stop()
    await wait_for(lambda: follower.is_leader)
    await follower.stop()
    assert not follower.is_leader


async def test_run_job_reports_failures():
    async def boom():
        raise ValueError("no")

    result = await run_job(Job("boom", boom, IntervalTrigger(hours=1), "fails"))
    assert result == {"job": "boom", "ok": False, "error": "ValueError"}


def test_registry_refuses_duplicates():
    with pytest.raises(RuntimeError):
        job("sweep_5min", IntervalTrigger(minutes=5), "dup")(lambda: None)


def test_architecture_jobs_are_registered():
    assert {"sweep_5min", "hourly_cleanup", "osm_refresh", "courier_statements"} <= set(JOBS)
    assert JOBS["osm_refresh"].enabled is False


async def test_manual_run_needs_admin_or_cron_token(client, factory):
    user, admin = await factory.user(), await factory.user(role="admin")
    anonymous = await client.post("/api/admin/jobs/sweep_5min/run")
    assert anonymous.status_code == 403
    assert (await client.post("/api/admin/jobs/sweep_5min/run", headers=auth(user))).status_code == 403
    wrong = await client.post("/api/admin/jobs/sweep_5min/run", headers={"x-cron-token": "nope"})
    assert wrong.status_code == 403

    by_cron = await client.post(
        "/api/admin/jobs/sweep_5min/run", headers={"x-cron-token": "test-cron-secret"}
    )
    assert by_cron.status_code == 200
    assert by_cron.json()["ok"] is True and by_cron.json()["result"] == {"implemented": False}
    listed = await client.get("/api/admin/jobs", headers=auth(admin))
    assert {j["name"] for j in listed.json()} >= {"sweep_5min", "courier_statements"}
    missing = await client.post("/api/admin/jobs/nope/run", headers=auth(admin))
    assert missing.status_code == 404 and error_of(missing) == "not_found"
    for name in ("hourly_cleanup", "osm_refresh", "courier_statements"):
        assert (await client.post(f"/api/admin/jobs/{name}/run", headers=auth(admin))).json()["ok"] is True
