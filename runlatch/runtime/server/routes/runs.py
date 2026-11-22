"""Workflow catalog, run trigger, listing, detail, cancel, replay, and SSE streaming routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from runlatch.core.types import RunCheckpoint, RunStatus, utc_now
from runlatch.runtime.server.auth import require_bearer_auth
from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/api/v1", tags=["Runs"])

WORKFLOW_CATALOG: list[dict[str, Any]] = [
    {
        "name": "pr-review",
        "description": "Autonomous Pull Request diff & security reviewer",
        "steps": ["gather_context", "security_and_code_review"],
        "default_max_cost_usd": 0.15,
    },
    {
        "name": "issue-to-pr",
        "description": "Autonomous issue patcher with stateless git worktree HITL gate",
        "steps": ["plan", "publish"],
        "default_max_cost_usd": 0.50,
    },
    {
        "name": "ci-triage",
        "description": "CI workflow failure root-cause investigator",
        "steps": ["extract_logs", "diagnose_failure"],
        "default_max_cost_usd": 0.20,
    },
    {
        "name": "dep-audit",
        "description": "Scheduled dependency vulnerability scanner",
        "steps": ["scan_dependencies", "summarize_findings"],
        "default_max_cost_usd": 0.15,
    },
    {
        "name": "doc-sync",
        "description": "Public API to documentation drift verifier",
        "steps": ["inspect_api_diff", "propose_doc_patch"],
        "default_max_cost_usd": 0.20,
    },
]

REGISTERED_WORKFLOW_NAMES = {w["name"] for w in WORKFLOW_CATALOG}


class TriggerRunRequest(BaseModel):
    """Payload for `POST /api/v1/runs`."""

    workflow_name: str
    repository: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    max_cost_usd: float = Field(default=0.25, gt=0.0, le=50.0)
    notify_channels: list[str] = Field(default_factory=list)


@router.get("/workflows")
async def list_workflows() -> list[dict[str, Any]]:
    """List registered workflows, steps, and default budgets."""
    return WORKFLOW_CATALOG


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    req: TriggerRunRequest,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    _token: Annotated[str, Depends(require_bearer_auth)],
) -> dict[str, Any]:
    """Enqueue a new workflow run asynchronously."""
    if req.workflow_name not in REGISTERED_WORKFLOW_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown workflow '{req.workflow_name}'.",
        )

    run_id = uuid4()
    now = utc_now()
    payload = dict(req.input)
    if req.repository:
        payload.setdefault("repository", req.repository)
    if req.notify_channels:
        payload["notify_channels"] = req.notify_channels

    cp = RunCheckpoint(
        run_id=run_id,
        workflow_name=req.workflow_name,
        status=RunStatus.QUEUED,
        trigger_source="api",
        repository=req.repository,
        input_payload=payload,
        max_cost_usd=req.max_cost_usd,
        created_at=now,
    )
    await ctx.store.save_checkpoint(cp)
    task_id = await ctx.dispatcher.dispatch_run(run_id, req.workflow_name, payload)

    return {
        "run_id": str(run_id),
        "workflow_name": req.workflow_name,
        "status": RunStatus.QUEUED.value,
        "task_id": task_id,
        "stream_url": f"/api/v1/runs/{run_id}/stream",
        "created_at": now.isoformat(),
    }


@router.get("/runs")
async def list_runs(
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    run_status: Annotated[str | None, Query(alias="status")] = None,
    workflow_name: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """List workflow runs with optional status and workflow_name filters."""
    checkpoints = await ctx.store.list_checkpoints(
        status=run_status, workflow_name=workflow_name, limit=limit
    )
    return [
        {
            "run_id": str(cp.run_id),
            "workflow_name": cp.workflow_name,
            "status": cp.status.value,
            "trigger_source": cp.trigger_source,
            "repository": cp.repository,
            "current_step_index": cp.current_step_index,
            "steps_taken": cp.steps_taken,
            "total_input_tokens": cp.total_input_tokens,
            "total_output_tokens": cp.total_output_tokens,
            "total_tokens": cp.total_input_tokens + cp.total_output_tokens,
            "total_cost_usd": cp.total_cost_usd,
            "max_cost_usd": cp.max_cost_usd,
            "pending_approval_id": str(cp.pending_approval_id)
            if cp.pending_approval_id
            else None,
            "created_at": cp.created_at.isoformat(),
        }
        for cp in checkpoints
    ]


@router.get("/runs/{run_id}")
async def get_run_detail(
    run_id: UUID,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> dict[str, Any]:
    """Return full run details including steps, tool calls, tokens, and USD cost."""
    cp = await ctx.store.get_checkpoint(run_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )
    steps = await ctx.store.list_steps(run_id)
    tool_calls = await ctx.store.list_tool_calls(run_id)
    return {
        "run_id": str(cp.run_id),
        "workflow_name": cp.workflow_name,
        "status": cp.status.value,
        "trigger_source": cp.trigger_source,
        "repository": cp.repository,
        "input_payload": cp.input_payload,
        "context_state": cp.context_state,
        "current_step_index": cp.current_step_index,
        "steps_taken": cp.steps_taken,
        "total_input_tokens": cp.total_input_tokens,
        "total_output_tokens": cp.total_output_tokens,
        "total_tokens": cp.total_input_tokens + cp.total_output_tokens,
        "total_cost_usd": cp.total_cost_usd,
        "max_cost_usd": cp.max_cost_usd,
        "pending_approval_id": str(cp.pending_approval_id)
        if cp.pending_approval_id
        else None,
        "workspace_meta": cp.workspace_meta.model_dump() if cp.workspace_meta else None,
        "has_workspace_patch": bool(cp.workspace_patch),
        "error_message": cp.error_message,
        "steps": [s.model_dump(mode="json") for s in steps],
        "tool_calls": [t.model_dump(mode="json") for t in tool_calls],
        "created_at": cp.created_at.isoformat(),
        "completed_at": cp.completed_at.isoformat() if cp.completed_at else None,
    }


@router.get("/runs/{run_id}/stream")
async def stream_run_events(
    run_id: UUID,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> StreamingResponse:
    """Server-Sent Events (SSE) stream of run status, steps, and tool calls."""
    cp = await ctx.store.get_checkpoint(run_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )

    steps = await ctx.store.list_steps(run_id)
    tool_calls = await ctx.store.list_tool_calls(run_id)

    async def _event_generator() -> AsyncIterator[str]:
        snapshot = {
            "event": "run.snapshot",
            "run_id": str(cp.run_id),
            "workflow_name": cp.workflow_name,
            "status": cp.status.value,
            "steps_count": len(steps),
            "tool_calls_count": len(tool_calls),
            "total_tokens": cp.total_input_tokens + cp.total_output_tokens,
            "total_cost_usd": cp.total_cost_usd,
        }
        yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
        for tc in tool_calls:
            tc_data = {
                "call_id": tc.call_id,
                "tool_name": tc.tool_name,
                "risk_tier": tc.risk_tier.value,
                "status": tc.status.value,
                "latency_ms": tc.latency_ms,
            }
            yield f"event: tool_call\ndata: {json.dumps(tc_data)}\n\n"

        if cp.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return

    return StreamingResponse(_event_generator(), media_type="text/event-stream")


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    _token: Annotated[str, Depends(require_bearer_auth)],
) -> dict[str, str]:
    """Cancel a queued, running, or paused workflow run."""
    cp = await ctx.store.get_checkpoint(run_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )
    if cp.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is already terminal ({cp.status.value}).",
        )
    cp.status = RunStatus.CANCELLED
    cp.completed_at = utc_now()
    await ctx.store.save_checkpoint(cp)
    return {"run_id": str(run_id), "status": RunStatus.CANCELLED.value}


@router.post("/runs/{run_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay_run(
    run_id: UUID,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    _token: Annotated[str, Depends(require_bearer_auth)],
) -> dict[str, Any]:
    """Clone input payload from an existing run and enqueue a fresh replay run."""
    source_cp = await ctx.store.get_checkpoint(run_id)
    if source_cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found."
        )

    new_run_id = uuid4()
    now = utc_now()
    new_cp = RunCheckpoint(
        run_id=new_run_id,
        workflow_name=source_cp.workflow_name,
        status=RunStatus.QUEUED,
        trigger_source="api",
        repository=source_cp.repository,
        input_payload=dict(source_cp.input_payload),
        max_cost_usd=source_cp.max_cost_usd,
        created_at=now,
    )
    await ctx.store.save_checkpoint(new_cp)
    task_id = await ctx.dispatcher.dispatch_run(
        new_run_id, new_cp.workflow_name, new_cp.input_payload
    )
    return {
        "run_id": str(new_run_id),
        "replayed_from": str(run_id),
        "workflow_name": new_cp.workflow_name,
        "status": RunStatus.QUEUED.value,
        "task_id": task_id,
        "stream_url": f"/api/v1/runs/{new_run_id}/stream",
        "created_at": now.isoformat(),
    }
