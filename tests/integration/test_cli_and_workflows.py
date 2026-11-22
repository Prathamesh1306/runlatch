"""Integration tests for CLI commands (`init`, `run --demo`, `runs`, `approvals`, `approve`)."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from runlatch.runtime.cli.main import app

runner = CliRunner()


def test_cli_init_and_pr_review_demo(tmp_path: Path) -> None:
    # 1. `runlatch init` creates runlatch.toml
    res_init = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res_init.exit_code == 0
    assert (tmp_path / "runlatch.toml").is_file()

    # 2. `runlatch run pr-review --demo` runs cleanly at $0.0000
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    res_run = runner.invoke(
        app,
        [
            "run",
            "pr-review",
            "--demo",
            "--workspace",
            str(Path.cwd()),
            "--db",
            db_url,
        ],
    )
    assert res_run.exit_code == 0, res_run.stdout
    assert "COMPLETED" in res_run.stdout
    assert "$0.0000" in res_run.stdout
    assert "Autonomous PR Review Report" in res_run.stdout

    # 3. `runlatch runs list` shows the completed run
    res_list = runner.invoke(app, ["runs", "list", "--db", db_url])
    assert res_list.exit_code == 0
    assert "pr-review" in res_list.stdout


def test_cli_issue_to_pr_demo_hitl_pause_approve_inspect_and_replay(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'hitl_cli.db'}"

    # 1. Run `issue-to-pr --demo` -> pauses on github_create_pr (WAITING_APPROVAL)
    res_pause = runner.invoke(
        app,
        [
            "run",
            "issue-to-pr",
            "--demo",
            "--issue",
            "108",
            "--workspace",
            str(Path.cwd()),
            "--db",
            db_url,
        ],
    )
    assert res_pause.exit_code == 0, res_pause.stdout
    assert "WAITING_APPROVAL" in res_pause.stdout

    match = re.search(
        r"runlatch approve ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        res_pause.stdout,
    )
    assert match is not None, f"Approval UUID not found in output:\n{res_pause.stdout}"
    approval_id = match.group(1)

    # 2. `runlatch approvals list` displays the pending approval
    res_approvals = runner.invoke(app, ["approvals", "list", "--db", db_url])
    assert res_approvals.exit_code == 0
    assert "github_create_pr" in res_approvals.stdout

    # 3. `runlatch approve <approval_id>` (without --demo) auto-detects demo from checkpoint
    res_approve = runner.invoke(
        app,
        [
            "approve",
            approval_id,
            "--reason",
            "Ship it",
            "--by",
            "prathamesh",
            "--workspace",
            str(Path.cwd()),
            "--db",
            db_url,
        ],
    )
    assert res_approve.exit_code == 0, res_approve.stdout
    assert "COMPLETED" in res_approve.stdout
    assert "https://github.com/demo/repo/pull/109" in res_approve.stdout

    # 4. Verify approve/reject/replay default `demo` parameter is False in signature
    import inspect

    from runlatch.runtime.cli.main import approve_cmd, reject_cmd, replay_cmd

    for fn in (approve_cmd, reject_cmd, replay_cmd):
        sig = inspect.signature(fn)
        assert sig.parameters["demo"].default is False
