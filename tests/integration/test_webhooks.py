"""Integration tests for Slice 5: FastAPI Control Plane, Webhooks, Safe HTMX UI, and Concurrency."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    RiskTier,
    RunCheckpoint,
    RunStatus,
    ToolCall,
    utc_now,
)
from runlatch.runtime.server.app import create_app
from runlatch.runtime.server.auth import (
    build_signed_approval_url,
    compute_github_signature,
)
from runlatch.runtime.server.deps import InProcessTaskDispatcher


def _make_test_app(
    tmp_path: Path,
) -> tuple[
    httpx.ASGITransport,
    SQLAlchemyCheckpointStore,
    InProcessTaskDispatcher,
    RunLatchSettings,
]:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'server.db'}"
    settings = RunLatchSettings(
        database_url=db_url,
        api_key="test-bearer-secret-key",
        github_webhook_secret="test-gh-webhook-secret",
        approval_signing_secret="test-approval-signing-secret-key",
        public_base_url="http://testserver",
    )
    store = SQLAlchemyCheckpointStore(db_url)
    dispatcher = InProcessTaskDispatcher()
    app = create_app(settings=settings, store=store, dispatcher=dispatcher)
    return httpx.ASGITransport(app=app), store, dispatcher, settings


@pytest.mark.asyncio
async def test_github_webhook_hmac_and_idempotency(tmp_path: Path) -> None:
    transport, store, dispatcher, settings = _make_test_app(tmp_path)
    await store.initialize()

    payload_bytes = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "Prathamesh1306/runlatch"},
            "pull_request": {"number": 42},
        }
    ).encode("utf-8")
    valid_sig = compute_github_signature(payload_bytes, settings.github_webhook_secret)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Invalid HMAC signature -> 401
        r_bad = await client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": "sha256=invalid",
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert r_bad.status_code == 401

        # 2. First valid delivery -> 202 Accepted and dispatches pr-review run
        r_ok = await client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": valid_sig,
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert r_ok.status_code == 202
        data_1 = r_ok.json()
        assert data_1["status"] == "accepted"
        assert data_1["workflow_name"] == "pr-review"
        assert len(dispatcher.dispatched_runs) == 1

        # 3. Duplicate delivery with same X-GitHub-Delivery -> 200 duplicate_ignored
        r_dup = await client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": valid_sig,
                "X-GitHub-Delivery": "deliv-1",
                "X-GitHub-Event": "pull_request",
            },
        )
        assert r_dup.status_code == 200
        assert r_dup.json()["status"] == "duplicate_ignored"
        assert len(dispatcher.dispatched_runs) == 1

    await store.dispose()


@pytest.mark.asyncio
async def test_ui_approval_get_never_mutates_and_concurrent_post_returns_200_and_409(
    tmp_path: Path,
) -> None:
    transport, store, dispatcher, settings = _make_test_app(tmp_path)
    await store.initialize()

    cp = RunCheckpoint(
        workflow_name="issue-to-pr",
        status=RunStatus.WAITING_APPROVAL,
        pending_tool_call=ToolCall(
            id="tc_pr", name="github_create_pr", arguments={"title": "fix"}
        ),
        workspace_patch="diff --git a/app.py b/app.py\n+x = 1\n",
    )
    approval = ApprovalRequestRecord(
        run_id=cp.run_id,
        call_id="tc_pr",
        tool_name="github_create_pr",
        risk_tier=RiskTier.EXTERNAL,
        sanitized_args={"title": "fix"},
        diff_preview=cp.workspace_patch,
        status=ApprovalStatus.PENDING,
        expires_at=utc_now() + timedelta(minutes=60),
    )
    cp.pending_approval_id = approval.id
    await store.save_checkpoint(cp)
    await store.create_approval(approval)

    signed_url = build_signed_approval_url(
        "http://testserver",
        approval.id,
        settings.approval_signing_secret,
    )
    relative_signed_url = signed_url.removeprefix("http://testserver")

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Simulate Slack/Email link-unfurler prefetching GET /ui/approvals/{id}?sig=...&exp=...
        get_resp = await client.get(relative_signed_url)
        assert get_resp.status_code == 200
        assert "github_create_pr" in get_resp.text
        assert "Approve &amp; Resume" in get_resp.text

        # Verify DB state was NOT mutated by GET!
        check_after_get = await store.get_approval(approval.id)
        assert check_after_get is not None
        assert check_after_get.status == ApprovalStatus.PENDING
        assert len(dispatcher.dispatched_resumes) == 0

        # 2. Fire 2 simultaneous POST /api/v1/approvals/{id}/decision requests
        headers = {"Authorization": f"Bearer {settings.api_key}"}
        payload = {"decision": "approve", "reason": "LGTM", "actor": "reviewer-1"}

        resp_a, resp_b = await asyncio.gather(
            client.post(
                f"/api/v1/approvals/{approval.id}/decision",
                json=payload,
                headers=headers,
            ),
            client.post(
                f"/api/v1/approvals/{approval.id}/decision",
                json=payload,
                headers=headers,
            ),
        )

        statuses = sorted([resp_a.status_code, resp_b.status_code])
        assert statuses == [200, 409], f"Expected [200, 409], got {statuses}"
        assert len(dispatcher.dispatched_resumes) == 1

    await store.dispose()


@pytest.mark.asyncio
async def test_rest_api_runs_sse_stream_metrics_and_ui_dashboard(tmp_path: Path) -> None:
    transport, store, _dispatcher, settings = _make_test_app(tmp_path)
    await store.initialize()
    headers = {"Authorization": f"Bearer {settings.api_key}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        assert len((await client.get("/api/v1/workflows")).json()) >= 2

        # Trigger run
        r_create = await client.post(
            "/api/v1/runs",
            json={
                "workflow_name": "pr-review",
                "repository": "Prathamesh1306/runlatch",
                "input": {"pr_number": 42},
                "max_cost_usd": 0.15,
            },
            headers=headers,
        )
        assert r_create.status_code == 202
        run_id = r_create.json()["run_id"]

        # Detail & list
        assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 200
        assert len((await client.get("/api/v1/runs")).json()) == 1

        # Cancel run so SSE stream finishes immediately after snapshot
        r_cancel = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        assert r_cancel.status_code == 200

        r_stream = await client.get(f"/api/v1/runs/{run_id}/stream")
        assert r_stream.status_code == 200
        assert "event: snapshot" in r_stream.text

        # Replay run
        r_replay = await client.post(f"/api/v1/runs/{run_id}/replay", headers=headers)
        assert r_replay.status_code == 202

        # Metrics & UI dashboard
        r_metrics = await client.get("/api/v1/metrics/summary")
        assert r_metrics.status_code == 200
        assert r_metrics.json()["total_runs"] == 2

        r_ui = await client.get("/ui")
        assert r_ui.status_code == 200
        assert "Live Run Inspector" in r_ui.text

    await store.dispose()
