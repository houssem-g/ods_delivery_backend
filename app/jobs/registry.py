"""Job registry. A job is an idempotent coroutine with a trigger; it opens its own transaction
(`app.db.transaction()`) and returns a small JSON-able summary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from apscheduler.triggers.base import BaseTrigger

JobFunc = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class Job:
    name: str
    func: JobFunc
    trigger: BaseTrigger
    description: str
    enabled: bool = True


JOBS: dict[str, Job] = {}


def job(
    name: str, trigger: BaseTrigger, description: str, enabled: bool = True
) -> Callable[[JobFunc], JobFunc]:
    """Decorator registering a scheduled job (also runnable by hand via /api/admin/jobs/{name}/run)."""

    def register(func: JobFunc) -> JobFunc:
        if name in JOBS:
            raise RuntimeError(f"job {name} registered twice")
        JOBS[name] = Job(name=name, func=func, trigger=trigger, description=description, enabled=enabled)
        return func

    return register
