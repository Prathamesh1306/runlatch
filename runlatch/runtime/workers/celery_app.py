"""Celery + Redis worker topology, Beat schedule, and `NullPool` async execution helper.

Enforces AGENTS.md §3.8:
Every Celery task calling `asyncio.run(...)` creates a task-scoped SQLAlchemy async
engine with `poolclass=NullPool` (`use_null_pool=True`) and calls `await store.dispose()`
in `finally`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings

T = TypeVar("T")

QUEUE_AGENT_RUNS = "agent.runs"
QUEUE_NOTIFICATIONS = "agent.notifications"
QUEUE_MAINTENANCE = "agent.maintenance"


def create_celery_app(settings: RunLatchSettings | None = None) -> Celery:
    """Create and configure the 3-queue Celery application."""
    cfg = settings or RunLatchSettings.load()
    app = Celery(
        "runlatch",
        broker=cfg.redis_url,
        backend=cfg.redis_url,
        include=[
            "runlatch.runtime.workers.agent_tasks",
            "runlatch.runtime.workers.notification_tasks",
            "runlatch.runtime.workers.maintenance_tasks",
        ],
    )
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_default_queue=QUEUE_AGENT_RUNS,
        task_queues=(
            Queue(QUEUE_AGENT_RUNS),
            Queue(QUEUE_NOTIFICATIONS),
            Queue(QUEUE_MAINTENANCE),
        ),
        task_routes={
            "runlatch.workers.run_workflow_task": {"queue": QUEUE_AGENT_RUNS},
            "runlatch.workers.resume_workflow_task": {"queue": QUEUE_AGENT_RUNS},
            "runlatch.workers.dispatch_notification_task": {
                "queue": QUEUE_NOTIFICATIONS
            },
            "runlatch.workers.expire_stale_approvals_task": {
                "queue": QUEUE_MAINTENANCE
            },
            "runlatch.workers.scheduled_dep_audit_task": {"queue": QUEUE_MAINTENANCE},
        },
        beat_schedule={
            "expire-stale-approvals-every-5m": {
                "task": "runlatch.workers.expire_stale_approvals_task",
                "schedule": 300.0,
                "options": {"queue": QUEUE_MAINTENANCE},
            },
            "weekly-dep-audit": {
                "task": "runlatch.workers.scheduled_dep_audit_task",
                "schedule": crontab(minute=0, hour=9, day_of_week=1),
                "options": {"queue": QUEUE_MAINTENANCE},
            },
        },
    )
    return app


celery_app = create_celery_app()


async def execute_with_null_pool(
    fn: Callable[[SQLAlchemyCheckpointStore, RunLatchSettings], Awaitable[T]],
    *,
    database_url: str | None = None,
    settings: RunLatchSettings | None = None,
) -> T:
    """Create a task-scoped `NullPool` store and dispose its engine in `finally`."""
    active_settings = settings or RunLatchSettings.load()
    db_url = database_url or active_settings.database_url
    store = SQLAlchemyCheckpointStore(db_url, use_null_pool=True)
    try:
        await store.initialize()
        return await fn(store, active_settings)
    finally:
        await store.dispose()


def run_sync_with_null_pool(
    fn: Callable[[SQLAlchemyCheckpointStore, RunLatchSettings], Awaitable[T]],
    *,
    database_url: str | None = None,
    settings: RunLatchSettings | None = None,
) -> T:
    """Synchronous Celery task bridge calling `asyncio.run(execute_with_null_pool(...))`."""
    return asyncio.run(
        execute_with_null_pool(fn, database_url=database_url, settings=settings)
    )


class CeleryTaskDispatcher:
    """Production TaskDispatcher sending tasks to Celery queues."""

    async def dispatch_run(
        self, run_id: UUID, workflow_name: str, payload: dict[str, Any]
    ) -> str:
        from runlatch.runtime.workers.agent_tasks import run_workflow_task

        async_result = run_workflow_task.apply_async(
            args=[str(run_id), workflow_name, payload],
            queue=QUEUE_AGENT_RUNS,
        )
        return str(async_result.id)

    async def dispatch_resume(self, run_id: UUID, approval_id: UUID) -> str:
        from runlatch.runtime.workers.agent_tasks import resume_workflow_task

        async_result = resume_workflow_task.apply_async(
            args=[str(run_id), str(approval_id)],
            queue=QUEUE_AGENT_RUNS,
        )
        return str(async_result.id)
