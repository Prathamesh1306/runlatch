"""GitHub webhook ingress with constant-time HMAC-SHA256 verification and Redis NX idempotency."""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from runlatch.core.types import RunCheckpoint, RunStatus, utc_now
from runlatch.runtime.server.auth import verify_github_signature
from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


def _match_github_workflow(
    event_type: str, payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    action = str(payload.get("action") or "")
    repo = str((payload.get("repository") or {}).get("full_name") or "unknown/repo")

    if event_type == "pull_request" and action in ("opened", "synchronize", "reopened"):
        pr_num = int((payload.get("pull_request") or {}).get("number") or 0)
        return "pr-review", {"repository": repo, "pr_number": pr_num}

    if event_type == "issues" and action in ("opened", "labeled"):
        issue_num = int((payload.get("issue") or {}).get("number") or 0)
        return "issue-to-pr", {"repository": repo, "issue_number": issue_num}

    if event_type == "workflow_run" and action == "completed":
        wf_run = payload.get("workflow_run") or {}
        if wf_run.get("conclusion") == "failure":
            return "ci-triage", {
                "repository": repo,
                "workflow_run_id": int(wf_run.get("id") or 0),
            }

    if event_type == "push":
        return "doc-sync", {"repository": repo, "ref": str(payload.get("ref") or "")}

    return None, {"repository": repo}


@router.post("/github")
async def github_webhook(
    request: Request,
    response: Response,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Verify GitHub HMAC, deduplicate via Redis NX key, and enqueue matching workflow."""
    raw_body = await request.body()
    if not verify_github_signature(
        raw_body, x_hub_signature_256, ctx.settings.github_webhook_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Hub-Signature-256 header.",
        )

    delivery_id = (x_github_delivery or "").strip()
    if not delivery_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header.",
        )

    event_type = (x_github_event or "ping").strip()
    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON webhook payload.",
        ) from exc

    # 1. Fast Redis SET NX EX 86400 idempotency check
    claimed = await ctx.idempotency.claim_delivery(delivery_id, ttl_seconds=86400)
    if not claimed:
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate_ignored", "delivery_id": delivery_id}

    workflow_name, input_payload = _match_github_workflow(event_type, payload)
    repo_name = str(input_payload.get("repository") or "unknown/repo")

    default_channels = ctx.settings.parsed_notify_channels()
    if default_channels and "notify_channels" not in input_payload:
        input_payload["notify_channels"] = default_channels

    if workflow_name is None:
        inserted = await ctx.store.record_webhook_event(
            provider="github",
            delivery_id=delivery_id,
            event_type=event_type,
            repository=repo_name,
            payload=payload,
            status="IGNORED",
            triggered_run_id=None,
        )
        if not inserted:
            response.status_code = status.HTTP_200_OK
            return {"status": "duplicate_ignored", "delivery_id": delivery_id}
        response.status_code = status.HTTP_200_OK
        return {"status": "ignored", "event_type": event_type, "delivery_id": delivery_id}

    run_id = uuid4()
    cp = RunCheckpoint(
        run_id=run_id,
        workflow_name=workflow_name,
        status=RunStatus.QUEUED,
        trigger_source="github_webhook",
        repository=repo_name,
        input_payload=input_payload,
        created_at=utc_now(),
    )
    await ctx.store.save_checkpoint(cp)

    inserted = await ctx.store.record_webhook_event(
        provider="github",
        delivery_id=delivery_id,
        event_type=event_type,
        repository=repo_name,
        payload=payload,
        status="ACCEPTED",
        triggered_run_id=run_id,
    )
    if not inserted:
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate_ignored", "delivery_id": delivery_id}

    task_id = await ctx.dispatcher.dispatch_run(run_id, workflow_name, input_payload)
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "workflow_name": workflow_name,
        "run_id": str(run_id),
        "task_id": task_id,
    }
