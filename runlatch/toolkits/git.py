"""GitToolkit providing shell=False git operations and stateless worktree helpers.

Imports ONLY Python stdlib, runlatch.core.*, and runlatch.tools.*.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from runlatch.core.exceptions import RunLatchError
from runlatch.core.protocols import RegisteredTool
from runlatch.core.types import RiskTier, WorkspaceMeta
from runlatch.tools.git_patcher import MAX_PATCH_BYTES, GitWorkspacePatcher
from runlatch.tools.registry import FunctionTool


class GitToolkit:
    """Safe git operations (`shell=False`) and worktree patch helpers."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        max_patch_bytes: int = MAX_PATCH_BYTES,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.patcher = GitWorkspacePatcher(
            self.workspace_root, max_patch_bytes=max_patch_bytes
        )

    async def _git(self, *args: str, cwd: Path | None = None) -> str:
        target_cwd = cwd or self.workspace_root
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(target_cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        out = out_b.decode("utf-8", errors="replace").strip()
        err = err_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RunLatchError(f"git {' '.join(args)} failed: {err or out}")
        return out

    async def git_status(self) -> str:
        """Return short git status of the repository workspace."""
        out = await self._git("status", "--short", "--branch")
        return out or "Clean working tree."

    async def git_diff(self) -> str:
        """Capture full unified diff against HEAD (runs `git add -N .` first, max 512 KB)."""
        patch, _ = await self.patcher.capture_patch()
        return patch or "(no diff against HEAD)"

    async def git_log(self, limit: int = 10) -> str:
        """Return recent commit history in oneline format."""
        safe_limit = min(max(limit, 1), 100)
        return await self._git("log", "--oneline", f"-n{safe_limit}")

    async def git_create_branch(self, branch_name: str) -> str:
        """Create and switch to a new git branch."""
        clean = branch_name.strip()
        if not clean or clean.startswith("-"):
            raise RunLatchError(f"Invalid branch name: '{branch_name}'")
        await self._git("checkout", "-b", clean)
        return f"Switched to new branch '{clean}'."

    async def git_commit(self, message: str) -> str:
        """Stage all changes and create a git commit."""
        clean_msg = message.strip()
        if not clean_msg:
            raise RunLatchError("Commit message must not be empty.")
        await self._git("add", "-A")
        return await self._git("commit", "-m", clean_msg)

    async def git_push(self, remote: str = "origin", branch: str = "HEAD") -> str:
        """Push commits to a remote repository (RiskTier.EXTERNAL, requires approval)."""
        if remote.startswith("-") or branch.startswith("-"):
            raise RunLatchError("Invalid remote or branch argument.")
        return await self._git("push", remote, branch)

    async def capture_patch(self) -> tuple[str, WorkspaceMeta]:
        """Delegate stateless workspace patch capture to GitWorkspacePatcher."""
        return await self.patcher.capture_patch()

    async def apply_patch_in_worktree(
        self, patch: str, meta: WorkspaceMeta, target_dir: Path | None = None
    ) -> Path:
        """Reconstruct base commit and patch in a fresh detached git worktree."""
        return await self.patcher.apply_patch_in_worktree(patch, meta, target_dir=target_dir)

    def tools(self) -> list[RegisteredTool]:
        """Return RegisteredTool instances for this toolkit."""
        return [
            FunctionTool(self.git_status, name="git_status", risk=RiskTier.READ),
            FunctionTool(self.git_diff, name="git_diff", risk=RiskTier.READ),
            FunctionTool(self.git_log, name="git_log", risk=RiskTier.READ),
            FunctionTool(
                self.git_create_branch, name="git_create_branch", risk=RiskTier.WRITE
            ),
            FunctionTool(self.git_commit, name="git_commit", risk=RiskTier.WRITE),
            FunctionTool(
                self.git_push,
                name="git_push",
                risk=RiskTier.EXTERNAL,
                requires_approval=True,
            ),
        ]
