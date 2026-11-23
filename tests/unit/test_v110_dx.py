"""Unit tests for v1.1.0 developer-experience polish."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    RiskTier,
    RunCheckpoint,
    RunStatus,
    ToolCall,
)
from runlatch.runtime.cli.main import app as cli_app
from runlatch.runtime.server.app import create_app
from runlatch.runtime.server.auth import compute_github_signature
from runlatch.runtime.server.deps import InProcessTaskDispatcher
from runlatch.runtime.server.routes.ui import parse_unified_diff
from runlatch.toolkits.git import GitToolkit


def test_parsed_notify_channels_from_settings_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comma-separated notify channels should strip blanks cleanly."""
    settings = RunLatchSettings(
        default_notify_channels=" slack:default , , email:dev@company.com ",
    )
    assert settings.parsed_notify_channels() == [
        "slack:default",
        "email:dev@company.com",
    ]
    assert RunLatchSettings(default_notify_channels="").parsed_notify_channels() == []

    monkeypatch.setenv(
        "RUNLATCH_DEFAULT_NOTIFY_CHANNELS",
        "discord:default,email:oncall@example.com",
    )
    loaded = RunLatchSettings()
    assert loaded.parsed_notify_channels() == [
        "discord:default",
        "email:oncall@example.com",
    ]


@pytest.mark.asyncio
async def test_github_webhook_attaches_default_notify_channels(tmp_path: Path) -> None:
    """GitHub webhook runs should receive configured default_notify_channels."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'webhook_notify.db'}"
    settings = RunLatchSettings(
        database_url=db_url,
        api_key="test-bearer-secret-key",
        github_webhook_secret="test-gh-webhook-secret",
        approval_signing_secret="test-approval-signing-secret-key",
        public_base_url="http://testserver",
        default_notify_channels="slack:default, email:dev@company.com",
    )
    store = SQLAlchemyCheckpointStore(db_url)
    await store.initialize()
    dispatcher = InProcessTaskDispatcher()
    app = create_app(settings=settings, store=store, dispatcher=dispatcher)
    transport = httpx.ASGITransport(app=app)

    payload_bytes = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "Prathamesh1306/runlatch"},
            "pull_request": {"number": 7},
        }
    ).encode("utf-8")
    sig = compute_github_signature(payload_bytes, settings.github_webhook_secret)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/webhooks/github",
                content=payload_bytes,
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Delivery": "deliv-v110-1",
                    "X-GitHub-Event": "pull_request",
                },
            )
        assert resp.status_code == 202
        assert len(dispatcher.dispatched_runs) == 1
        _run_id, wf_name, dispatched_payload = dispatcher.dispatched_runs[0]
        assert wf_name == "pr-review"
        assert dispatched_payload["notify_channels"] == [
            "slack:default",
            "email:dev@company.com",
        ]
    finally:
        await store.dispose()


def test_docker_compose_has_optional_env_file_on_app_services() -> None:
    """All runlatch runtime services in docker-compose.yml should load .env optionally."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    for name in ("api", "worker-agents", "worker-notifications", "beat"):
        env_files = services[name].get("env_file")
        assert env_files == [{"path": ".env", "required": False}], name


def test_cli_init_server_scaffolds_compose_and_env_example(tmp_path: Path) -> None:
    """`runlatch init --server` should scaffold PyPI-ready server files safely."""
    runner = CliRunner()
    res = runner.invoke(cli_app, ["init", "--server", "--path", str(tmp_path)])
    assert res.exit_code == 0, res.output

    toml_file = tmp_path / "runlatch.toml"
    env_file = tmp_path / ".env.example"
    compose_file = tmp_path / "docker-compose.yml"
    assert toml_file.exists()
    assert env_file.exists()
    assert compose_file.exists()

    compose_text = compose_file.read_text(encoding="utf-8")
    assert "runlatch[mcp,server]" in compose_text
    assert "required: false" in compose_text
    compose_data = yaml.safe_load(compose_text)
    assert "api" in compose_data["services"]

    # Re-running without --force skips existing files without failing.
    compose_file.write_text("# custom compose\n", encoding="utf-8")
    res_skip = runner.invoke(cli_app, ["init", "--server", "--path", str(tmp_path)])
    assert res_skip.exit_code == 0, res_skip.output
    assert compose_file.read_text(encoding="utf-8") == "# custom compose\n"

    # Re-running with --force overwrites.
    res_force = runner.invoke(
        cli_app,
        ["init", "--server", "--force", "--path", str(tmp_path)],
    )
    assert res_force.exit_code == 0, res_force.output
    assert "runlatch[mcp,server]" in compose_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_diff_and_patcher_fallback_on_unborn_head(tmp_path: Path) -> None:
    """Zero-commit git repos should diff against empty tree instead of raising."""
    repo = tmp_path / "fresh_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dev@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    toolkit = GitToolkit(repo)
    assert await toolkit.git_diff() == "(no diff against HEAD)"

    (repo / "hello.py").write_text("print('hello runlatch')\n", encoding="utf-8")
    diff_text = await toolkit.git_diff()
    assert "hello.py" in diff_text
    assert "print('hello runlatch')" in diff_text

    patch, meta = await toolkit.patcher.capture_patch()
    assert "hello.py" in patch
    assert meta.base_commit_sha == ""
    assert meta.branch_name == "main"
    assert meta.workspace_root == str(repo)


def test_parse_unified_diff_line_numbers_and_stats() -> None:
    """Unified git diff parser should classify hunk/add/del/ctx lines and stats."""
    sample = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,3 +10,4 @@ def greet():\n"
        "     old_ctx = True\n"
        "-    return 'hi'\n"
        "+    msg = 'hello'\n"
        "+    return msg\n"
    )
    lines, stats = parse_unified_diff(sample)
    assert stats == {"added": 2, "removed": 1, "files": 1}
    kinds = [item["kind"] for item in lines]
    assert kinds == ["meta", "meta", "meta", "hunk", "ctx", "del", "add", "add"]
    assert lines[4]["old_num"] == "10" and lines[4]["new_num"] == "10"
    assert lines[5]["old_num"] == "11" and lines[5]["new_num"] == ""
    assert lines[6]["old_num"] == "" and lines[6]["new_num"] == "11"
    assert lines[7]["old_num"] == "" and lines[7]["new_num"] == "12"


@pytest.mark.asyncio
async def test_v111_ui_dashboard_and_approval_diff_viewer(tmp_path: Path) -> None:
    """Dashboard and approval UI should render SSE drawer, signed links, and diff rows."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ui_v111.db'}"
    settings = RunLatchSettings(
        database_url=db_url,
        api_key="test-bearer-secret-key",
        github_webhook_secret="test-gh-webhook-secret",
        approval_signing_secret="test-approval-signing-secret-key",
        public_base_url="http://testserver",
    )
    store = SQLAlchemyCheckpointStore(db_url)
    await store.initialize()
    dispatcher = InProcessTaskDispatcher()
    app = create_app(settings=settings, store=store, dispatcher=dispatcher)
    transport = httpx.ASGITransport(app=app)

    cp = RunCheckpoint(
        workflow_name="doc-sync",
        status=RunStatus.WAITING_APPROVAL,
        pending_tool_call=ToolCall(
            id="tc_doc",
            name="github_create_pr",
            arguments={"title": "docs: sync CLI flags"},
        ),
        workspace_patch=(
            "diff --git a/README.md b/README.md\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
        ),
    )
    approval = ApprovalRequestRecord(
        run_id=cp.run_id,
        call_id="tc_doc",
        tool_name="github_create_pr",
        risk_tier=RiskTier.EXTERNAL,
        sanitized_args={"title": "docs: sync CLI flags"},
        diff_preview=cp.workspace_patch,
        status=ApprovalStatus.PENDING,
    )
    cp.pending_approval_id = approval.id
    await store.save_checkpoint(cp)
    await store.create_approval(approval)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            r_ui = await client.get("/ui")
            assert r_ui.status_code == 200
            assert "Action Required" in r_ui.text
            assert f"/ui/approvals/{approval.id}?sig=" in r_ui.text
            assert 'id="sse-drawer"' in r_ui.text
            assert 'hx-select="#dashboard-body"' in r_ui.text
            assert "Watch Live" in r_ui.text
            assert str(cp.run_id)[:8] in r_ui.text

            r_confirm = await client.get(f"/ui/approvals/{approval.id}")
            assert r_confirm.status_code == 200
            assert "diff-line-add" in r_confirm.text
            assert "diff-line-del" in r_confirm.text
            assert "diff-line-hunk" in r_confirm.text
            assert 'id="decision-feedback"' in r_confirm.text
            assert "Approve &amp; Resume" in r_confirm.text

            # Verify GET never mutates approval status
            after = await store.get_approval(approval.id)
            assert after is not None and after.status == ApprovalStatus.PENDING
    finally:
        await store.dispose()

