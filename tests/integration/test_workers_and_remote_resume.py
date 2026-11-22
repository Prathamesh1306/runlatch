"""Integration tests for Slice 6: Celery workers, NullPool runner, and remote worktree resume."""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import NullPool

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    NotificationEvent,
    RiskTier,
    RunCheckpoint,
    RunStatus,
    utc_now,
)
from runlatch.runtime.workers.agent_tasks import (
    execute_workflow_resume_async,
    execute_workflow_run_async,
)
from runlatch.runtime.workers.celery_app import (
    QUEUE_AGENT_RUNS,
    QUEUE_MAINTENANCE,
    QUEUE_NOTIFICATIONS,
    celery_app,
    execute_with_null_pool,
)
from runlatch.runtime.workers.maintenance_tasks import expire_stale_approvals_task
from runlatch.runtime.workers.notification_tasks import (
    NOTIFICATION_BACKOFF_SECONDS,
    deliver_notification_async,
)
from runlatch.tools.git_patcher import GitWorkspacePatcher


def test_celery_topology_and_beat_schedule() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1

    queue_names = {q.name for q in celery_app.conf.task_queues}
    assert queue_names == {
        QUEUE_AGENT_RUNS,
        QUEUE_NOTIFICATIONS,
        QUEUE_MAINTENANCE,
    }
    assert NOTIFICATION_BACKOFF_SECONDS == (10, 30, 90, 270, 810)
    assert "expire-stale-approvals-every-5m" in celery_app.conf.beat_schedule
    assert "weekly-dep-audit" in celery_app.conf.beat_schedule


@pytest.mark.asyncio
async def test_null_pool_worker_runner_and_remote_clone_worktree_resume(
    tmp_path: Path,
) -> None:
    # 1. Create a bare origin repo and a Worker-1 clone
    origin_dir = tmp_path / "origin.git"
    origin_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare"], cwd=origin_dir, check=True, capture_output=True
    )

    worker1_dir = tmp_path / "worker1_repo"
    subprocess.run(
        ["git", "clone", str(origin_dir), str(worker1_dir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "w1@runlatch.dev"],
        cwd=worker1_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Worker 1"],
        cwd=worker1_dir,
        check=True,
        capture_output=True,
    )
    (worker1_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worker1_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=worker1_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD"],
        cwd=worker1_dir,
        check=True,
        capture_output=True,
    )

    # Worker 1 modifies a tracked file and adds an untracked file
    (worker1_dir / "module.py").write_text("VALUE = 42\n", encoding="utf-8")
    (worker1_dir / "added_on_worker1.py").write_text("NEW_FLAG = True\n", encoding="utf-8")

    # 2. Run issue-to-pr on Worker 1 until HITL pause
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'workers.db'}"

    async def _worker1_step(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> tuple[RunCheckpoint, ApprovalRequestRecord]:
        assert isinstance(store.engine.pool, NullPool)
        res = await execute_workflow_run_async(
            store,
            settings,
            run_id=RunCheckpoint().run_id,
            workflow_name="issue-to-pr",
            payload={"issue_number": 108, "demo": True},
            workspace_root=worker1_dir,
        )
        assert res.status == RunStatus.WAITING_APPROVAL
        assert res.approval_id is not None
        cp = await store.get_checkpoint(res.run_id)
        appr = await store.get_approval(res.approval_id)
        assert cp is not None and appr is not None
        return cp, appr

    cp, appr = await execute_with_null_pool(_worker1_step, database_url=db_url)
    assert cp.workspace_patch is not None
    assert "added_on_worker1.py" in cp.workspace_patch
    assert cp.workspace_meta is not None
    assert cp.workspace_meta.repo_url == str(origin_dir)

    # 3. Delete Worker 1's directory completely to simulate a stateless remote Worker 2 container!
    import shutil

    shutil.rmtree(worker1_dir)
    assert not worker1_dir.exists()

    # Worker 2 reconstructs the worktree by cloning `meta.repo_url` at `meta.base_commit_sha`!
    worker2_wt = tmp_path / "worker2_reconstructed_wt"
    remote_patcher = GitWorkspacePatcher(
        tmp_path / "nonexistent_local_path", worktree_target_dir=worker2_wt
    )
    restored_path = await remote_patcher.apply_patch_in_worktree(
        cp.workspace_patch,
        cp.workspace_meta.model_copy(update={"workspace_root": ""}),
        target_dir=worker2_wt,
    )
    assert (restored_path / "module.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    added_file = restored_path / "added_on_worker1.py"
    assert added_file.read_text(encoding="utf-8") == "NEW_FLAG = True\n"

    # 4. Approve and resume workflow on Worker 2 via execute_with_null_pool
    async def _worker2_resume(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> str:
        await store.decide_approval(
            appr.id, approved=True, decided_by="prathamesh", reason="Approved"
        )
        res = await execute_workflow_resume_async(
            store,
            settings,
            run_id=cp.run_id,
            approval_id=appr.id,
            workspace_root=tmp_path / "nonexistent_local_path",
            worktree_target_dir=tmp_path / "worker2_task_wt",
        )
        assert res.status == RunStatus.COMPLETED
        return res.output

    final_output = await execute_with_null_pool(_worker2_resume, database_url=db_url)
    assert "https://github.com/demo/repo/pull/109" in final_output


@pytest.mark.asyncio
async def test_notification_delivery_audit_and_stale_approval_expiration(
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'maint.db'}"
    store = SQLAlchemyCheckpointStore(db_url, use_null_pool=True)
    settings = RunLatchSettings(database_url=db_url)

    # 1. Create an already-expired PENDING approval + WAITING_APPROVAL run
    import asyncio

    cp = RunCheckpoint(workflow_name="issue-to-pr", status=RunStatus.WAITING_APPROVAL)
    expired_appr = ApprovalRequestRecord(
        run_id=cp.run_id,
        tool_name="github_create_pr",
        risk_tier=RiskTier.EXTERNAL,
        status=ApprovalStatus.PENDING,
        expires_at=utc_now() - timedelta(minutes=5),
    )
    await store.save_checkpoint(cp)
    await store.create_approval(expired_appr)

    sweep_res = await asyncio.to_thread(
        expire_stale_approvals_task, database_url=db_url
    )
    assert sweep_res["expired_count"] == 1

    updated_cp = await store.get_checkpoint(cp.run_id)
    assert updated_cp is not None
    assert updated_cp.status == RunStatus.CANCELLED

    # 2. Test notification delivery DEAD_LETTER logging on final retry failure
    event = NotificationEvent(
        event_type="run.failed",
        run_id=cp.run_id,
        workflow_name="issue-to-pr",
        title="Run Failed",
        summary="Failed summary",
    )
    deliv = await deliver_notification_async(
        store,
        settings,
        event_dict=event.model_dump(mode="json"),
        channel_spec="github:invalid-target-without-hash",
        attempt=5,
        max_attempts=5,
    )
    assert deliv["status"] == "DEAD_LETTER"
    rows = await store.list_notification_deliveries(run_id=cp.run_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "DEAD_LETTER"
    assert rows[0]["attempt_count"] == 5

    # 3. Verify Celery worker run enqueues dispatch_notification_task.delay() on agent.notifications
    enqueued_delays: list[tuple[tuple[object, ...], dict[str, object]]] = []

    from runlatch.runtime.workers import notification_tasks

    orig_delay = notification_tasks.dispatch_notification_task.delay

    def fake_delay(*args: object, **kwargs: object) -> object:
        enqueued_delays.append((args, kwargs))
        return None

    notification_tasks.dispatch_notification_task.delay = fake_delay  # type: ignore[method-assign]
    try:
        await execute_workflow_run_async(
            store,
            settings,
            run_id=RunCheckpoint().run_id,
            workflow_name="pr-review",
            payload={
                "pr_number": 42,
                "demo": True,
                "notify_channels": ["slack:default", "discord:default"],
            },
            use_celery_notifications=True,
        )
    finally:
        notification_tasks.dispatch_notification_task.delay = orig_delay  # type: ignore[method-assign]

    assert len(enqueued_delays) == 2
    assert enqueued_delays[0][0][1] == "slack:default"
    assert enqueued_delays[1][0][1] == "discord:default"
    await store.dispose()
