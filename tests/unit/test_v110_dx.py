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
from runlatch.runtime.cli.main import app as cli_app
from runlatch.runtime.server.app import create_app
from runlatch.runtime.server.auth import compute_github_signature
from runlatch.runtime.server.deps import InProcessTaskDispatcher
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
