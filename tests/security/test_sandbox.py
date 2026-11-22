"""Security tests for resolve_safe_path, denied globs, and SubprocessExecutor guardrails."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runlatch.adapters.executors.subprocess_exec import SubprocessExecutor
from runlatch.core.exceptions import SandboxViolationError
from runlatch.tools.sandbox import resolve_safe_path


def test_resolve_safe_path_blocks_traversal_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    # 1. '..' traversal blocked
    with pytest.raises(SandboxViolationError):
        resolve_safe_path(workspace, "../outside_secret.txt")

    # 2. Absolute path outside workspace blocked
    with pytest.raises(SandboxViolationError):
        resolve_safe_path(workspace, str(outside_file))

    # 3. Symlink inside workspace pointing outside blocked
    symlink_path = workspace / "evil_link.txt"
    os.symlink(outside_file, symlink_path)
    with pytest.raises(SandboxViolationError):
        resolve_safe_path(workspace, "evil_link.txt")


def test_resolve_safe_path_blocks_denied_globs(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    (workspace / "certs").mkdir()
    (workspace / "certs" / "prod.pem").write_text("PRIVATE", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]", encoding="utf-8")

    for forbidden in (".env", ".env.local", "certs/prod.pem", "id_rsa", ".git/config"):
        with pytest.raises(SandboxViolationError):
            resolve_safe_path(workspace, forbidden)


@pytest.mark.asyncio
async def test_subprocess_executor_blocks_interpreters_and_metacharacters(
    tmp_path: Path,
) -> None:
    # Cannot even instantiate SubprocessExecutor with raw 'python' or 'bash' in allowlist
    for raw_interp in ("python", "python3", "bash", "sh", "node", "curl"):
        with pytest.raises(SandboxViolationError):
            SubprocessExecutor(allowed_binaries=[raw_interp])

    executor = SubprocessExecutor(allowed_binaries=["git"])

    # Cannot execute unallowed binary
    with pytest.raises(SandboxViolationError):
        await executor.run_command(["ls", "-la"], cwd=tmp_path)

    # Cannot execute raw python even if attempted directly
    with pytest.raises(SandboxViolationError):
        await executor.run_command(["python3", "-c", "print(1)"], cwd=tmp_path)

    # Cannot pass shell metacharacters in arguments
    for injection in ("status; rm -rf /", "status && whoami", "status | cat", "$(id)"):
        with pytest.raises(SandboxViolationError):
            await executor.run_command(["git", injection], cwd=tmp_path)

    # Valid git command succeeds with shell=False
    res = await executor.run_command(["git", "--version"], cwd=tmp_path)
    assert res.exit_code == 0
    assert "git version" in res.stdout
