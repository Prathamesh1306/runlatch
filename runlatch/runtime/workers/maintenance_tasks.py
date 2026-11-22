"""Celery Beat maintenance tasks (`agent.maintenance` queue): approval sweeper & audits."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.types import RunCheckpoint, RunStatus, utc_now
from runlatch.runtime.workers.agent_tasks import execute_workflow_run_async
from runlatch.runtime.workers.celery_app import (
    QUEUE_MAINTENANCE,
    celery_app,
    run_sync_with_null_pool,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="runlatch.workers.expire_stale_approvals_task",
    queue=QUEUE_MAINTENANCE,
    max_retries=1,
    soft_time_limit=180,
    time_limit=240,
)
def expire_stale_approvals_task(database_url: str | None = None) -> dict[str, Any]:
    """Sweep PENDING approvals past `expires_at`, marking them EXPIRED and cancelling runs."""

    async def _op(
        store: SQLAlchemyCheckpointStore, _settings: RunLatchSettings
    ) -> dict[str, Any]:
        expired = await store.expire_stale_approvals()
        return {
            "expired_count": len(expired),
            "expired_approval_ids": [str(a.id) for a in expired],
        }

    return run_sync_with_null_pool(_op, database_url=database_url)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="runlatch.workers.scheduled_dep_audit_task",
    queue=QUEUE_MAINTENANCE,
    max_retries=1,
    soft_time_limit=180,
    time_limit=240,
)
def scheduled_dep_audit_task(
    repository: str = "local/repo",
    demo: bool = True,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Scheduled weekly dependency & security audit triggered by Celery Beat."""

    async def _op(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> dict[str, Any]:
        run_id = uuid4()
        payload = {"repository": repository, "pr_number": 1, "demo": demo}
        await store.save_checkpoint(
            RunCheckpoint(
                run_id=run_id,
                workflow_name="pr-review",
                status=RunStatus.QUEUED,
                trigger_source="schedule",
                repository=repository,
                input_payload=payload,
                created_at=utc_now(),
            )
        )
        res = await execute_workflow_run_async(
            store,
            settings,
            run_id=run_id,
            workflow_name="pr-review",
            payload=payload,
        )
        return {"run_id": str(res.run_id), "status": res.status.value}

    return run_sync_with_null_pool(_op, database_url=database_url)
