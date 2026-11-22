"""Celery workflow execution and stateless worktree resume tasks (`agent.runs` queue)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from runlatch.adapters.llm.anthropic import AnthropicProvider
from runlatch.adapters.llm.gemini import GeminiProvider
from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.protocols import LLMProvider
from runlatch.core.types import (
    ApprovalStatus,
    LLMResponse,
    NotificationEvent,
    RunResult,
    RunStatus,
)
from runlatch.core.workflow import Workflow
from runlatch.runtime.workers.celery_app import (
    QUEUE_AGENT_RUNS,
    celery_app,
    run_sync_with_null_pool,
)
from runlatch.runtime.workers.notification_tasks import (
    deliver_notification_async,
    dispatch_notification_task,
)
from runlatch.tools.git_patcher import GitWorkspacePatcher
from runlatch.workflows.ci_triage import (
    build_ci_triage_workflow,
    ci_triage_demo_responses,
)
from runlatch.workflows.dep_audit import (
    build_dep_audit_workflow,
    dep_audit_demo_responses,
)
from runlatch.workflows.doc_sync import (
    build_doc_sync_workflow,
    doc_sync_demo_responses,
)
from runlatch.workflows.issue_to_pr import (
    build_issue_to_pr_workflow,
    issue_to_pr_demo_responses,
)
from runlatch.workflows.pr_review import (
    build_pr_review_workflow,
    pr_review_demo_responses,
)


def _demo_responses(workflow_name: str, issue_num: int) -> list[LLMResponse]:
    if workflow_name == "issue-to-pr":
        return issue_to_pr_demo_responses(issue_num)
    if workflow_name == "ci-triage":
        return ci_triage_demo_responses()
    if workflow_name == "dep-audit":
        return dep_audit_demo_responses()
    if workflow_name == "doc-sync":
        return doc_sync_demo_responses()
    return pr_review_demo_responses()


def _select_worker_provider(
    workflow_name: str,
    payload: dict[str, Any],
    settings: RunLatchSettings,
    *,
    is_resume: bool = False,
) -> LLMProvider:
    issue_num = int(payload.get("issue_number", 108))
    use_demo = bool(payload.get("demo") is True) or settings.llm_provider in (
        "mock",
        "demo",
    )
    if use_demo:
        resps = _demo_responses(workflow_name, issue_num)
        return MockLLMProvider([resps[-1]] if is_resume else resps, model="demo")

    prov = settings.llm_provider.strip().lower()
    if prov == "ollama":
        return OpenAICompatibleProvider.ollama(
            model=settings.llm_model, base_url=settings.llm_base_url
        )
    if prov == "groq":
        return OpenAICompatibleProvider.groq(
            api_key=settings.groq_api_key or settings.llm_api_key
        )
    if prov in ("github", "github-models", "github_models"):
        return OpenAICompatibleProvider.github_models(
            token=settings.github_token or settings.llm_api_key
        )
    if prov in ("anthropic", "claude"):
        return AnthropicProvider(
            model=settings.llm_model
            if "claude" in settings.llm_model
            else "claude-3-5-sonnet-latest",
            api_key=settings.anthropic_api_key or settings.llm_api_key,
        )
    if prov in ("gemini", "google"):
        return GeminiProvider(
            model=settings.llm_model
            if "gemini" in settings.llm_model
            else "gemini-2.0-flash",
            api_key=settings.gemini_api_key or settings.llm_api_key,
        )
    return OpenAICompatibleProvider(
        model=settings.llm_model,
        api_key=settings.openai_api_key or settings.llm_api_key,
        base_url=settings.llm_base_url,
    )


def _construct_workflow(
    workflow_name: str,
    *,
    provider: LLMProvider,
    workspace_root: Path,
    max_cost_usd: float,
    store: SQLAlchemyCheckpointStore,
) -> Workflow:
    if workflow_name == "issue-to-pr":
        return build_issue_to_pr_workflow(
            provider,
            workspace_root=workspace_root,
            max_cost_usd=max_cost_usd,
            store=store,
        )
    if workflow_name == "ci-triage":
        return build_ci_triage_workflow(
            provider,
            workspace_root=workspace_root,
            max_cost_usd=max_cost_usd,
            store=store,
        )
    if workflow_name == "dep-audit":
        return build_dep_audit_workflow(
            provider,
            workspace_root=workspace_root,
            max_cost_usd=max_cost_usd,
            store=store,
        )
    if workflow_name == "doc-sync":
        return build_doc_sync_workflow(
            provider,
            workspace_root=workspace_root,
            max_cost_usd=max_cost_usd,
            store=store,
        )
    return build_pr_review_workflow(
        provider,
        workspace_root=workspace_root,
        max_cost_usd=max_cost_usd,
        store=store,
    )


async def _notify_for_result(
    store: SQLAlchemyCheckpointStore,
    settings: RunLatchSettings,
    result: RunResult,
    channels: list[str],
    *,
    use_celery_queue: bool = True,
) -> None:
    """Dispatch notifications via the isolated `agent.notifications` queue (or in-process)."""
    if not channels:
        return
    if result.status == RunStatus.WAITING_APPROVAL:
        event = NotificationEvent(
            event_type="approval.requested",
            run_id=result.run_id,
            workflow_name=result.workflow_name,
            title=f"Approval Required: {result.workflow_name}",
            summary=result.output,
            metadata={"approval_id": str(result.approval_id)},
        )
    elif result.status == RunStatus.COMPLETED:
        event = NotificationEvent(
            event_type="run.completed",
            run_id=result.run_id,
            workflow_name=result.workflow_name,
            title=f"Workflow Completed: {result.workflow_name}",
            summary=result.output[:500],
        )
    else:
        return

    event_dict = event.model_dump(mode="json")
    approval_id_str = str(result.approval_id) if result.approval_id else None
    for ch_spec in channels:
        if use_celery_queue:
            dispatch_notification_task.delay(
                event_dict,
                ch_spec,
                approval_id=approval_id_str,
                database_url=store.database_url,
            )
        else:
            await deliver_notification_async(
                store,
                settings,
                event_dict=event_dict,
                channel_spec=ch_spec,
                approval_id=approval_id_str,
            )


async def execute_workflow_run_async(
    store: SQLAlchemyCheckpointStore,
    settings: RunLatchSettings,
    *,
    run_id: UUID,
    workflow_name: str,
    payload: dict[str, Any],
    workspace_root: Path | None = None,
    use_celery_notifications: bool = False,
) -> RunResult:
    """Run a workflow asynchronously using a task-scoped NullPool CheckpointStore."""
    cp = await store.get_checkpoint(run_id)
    max_cost = cp.max_cost_usd if cp else settings.max_cost_usd
    ws_root = workspace_root or Path(str(payload.get("workspace_root") or ".")).resolve()

    provider = _select_worker_provider(workflow_name, payload, settings, is_resume=False)
    wf = _construct_workflow(
        workflow_name,
        provider=provider,
        workspace_root=ws_root,
        max_cost_usd=max_cost,
        store=store,
    )
    result = await wf.run(payload, run_id=run_id, store=store)
    channels = [str(c) for c in (payload.get("notify_channels") or [])]
    await _notify_for_result(
        store,
        settings,
        result,
        channels,
        use_celery_queue=use_celery_notifications,
    )
    return result


async def execute_workflow_resume_async(
    store: SQLAlchemyCheckpointStore,
    settings: RunLatchSettings,
    *,
    run_id: UUID,
    approval_id: UUID,
    workspace_root: Path | None = None,
    worktree_target_dir: Path | None = None,
    use_celery_notifications: bool = False,
) -> RunResult:
    """Statelessly reconstruct git worktree from `workspace_patch` and resume workflow."""
    cp = await store.get_checkpoint(run_id)
    if cp is None:
        raise RuntimeError(f"Checkpoint {run_id} not found.")
    approval = await store.get_approval(approval_id)
    if approval is None:
        raise RuntimeError(f"Approval {approval_id} not found.")

    approved = approval.status == ApprovalStatus.APPROVED
    ws_root = workspace_root or Path(
        (cp.workspace_meta.workspace_root if cp.workspace_meta else "") or "."
    )

    active_workspace = ws_root
    if approved and cp.workspace_patch is not None and cp.workspace_meta is not None:
        patcher = GitWorkspacePatcher(
            ws_root, worktree_target_dir=worktree_target_dir
        )
        active_workspace = await patcher.apply_patch_in_worktree(
            cp.workspace_patch, cp.workspace_meta, target_dir=worktree_target_dir
        )

    if cp.status == RunStatus.RESUMING:
        cp.status = RunStatus.WAITING_APPROVAL
        cp.pending_approval_id = None
        await store.save_checkpoint(cp)

    provider = _select_worker_provider(
        cp.workflow_name, cp.input_payload, settings, is_resume=True
    )
    wf = _construct_workflow(
        cp.workflow_name,
        provider=provider,
        workspace_root=active_workspace,
        max_cost_usd=cp.max_cost_usd,
        store=store,
    )
    result = await wf.resume(
        run_id,
        approved=approved,
        decided_by=approval.decided_by or "reviewer",
        reason=approval.decision_reason or "",
        store=store,
    )
    channels = [str(c) for c in (cp.input_payload.get("notify_channels") or [])]
    await _notify_for_result(
        store,
        settings,
        result,
        channels,
        use_celery_queue=use_celery_notifications,
    )
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    name="runlatch.workers.run_workflow_task",
    bind=True,
    queue=QUEUE_AGENT_RUNS,
    max_retries=2,
    soft_time_limit=540,
    time_limit=600,
)
def run_workflow_task(
    self: Any,
    run_id_str: str,
    workflow_name: str,
    payload: dict[str, Any],
    database_url: str | None = None,
) -> dict[str, Any]:
    """Celery worker entry point for `agent.runs` fresh workflow execution."""
    del self
    run_id = UUID(run_id_str)

    async def _op(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> dict[str, Any]:
        res = await execute_workflow_run_async(
            store,
            settings,
            run_id=run_id,
            workflow_name=workflow_name,
            payload=payload,
            use_celery_notifications=True,
        )
        return {
            "run_id": str(res.run_id),
            "status": res.status.value,
            "approval_id": str(res.approval_id) if res.approval_id else None,
            "total_tokens": res.total_tokens,
            "total_cost_usd": res.total_cost_usd,
        }

    return run_sync_with_null_pool(_op, database_url=database_url)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="runlatch.workers.resume_workflow_task",
    bind=True,
    queue=QUEUE_AGENT_RUNS,
    max_retries=2,
    soft_time_limit=540,
    time_limit=600,
)
def resume_workflow_task(
    self: Any,
    run_id_str: str,
    approval_id_str: str,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Celery worker entry point for `agent.runs` stateless worktree resume."""
    del self
    run_id = UUID(run_id_str)
    approval_id = UUID(approval_id_str)

    async def _op(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> dict[str, Any]:
        res = await execute_workflow_resume_async(
            store,
            settings,
            run_id=run_id,
            approval_id=approval_id,
            use_celery_notifications=True,
        )
        return {
            "run_id": str(res.run_id),
            "status": res.status.value,
            "output": res.output,
            "total_tokens": res.total_tokens,
            "total_cost_usd": res.total_cost_usd,
        }

    return run_sync_with_null_pool(_op, database_url=database_url)
