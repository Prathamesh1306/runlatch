"""Stateless git workspace patch capture (git add -N . + git diff HEAD) and worktree reconstruction.

Implements WorkspacePatcher protocol from runlatch.core.protocols.
Supports both local worktree creation and remote worker clone+checkout when the original
workspace_root does not exist on the resuming worker container.
Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from runlatch.core.exceptions import PatchTooLargeError, RunLatchError
from runlatch.core.types import WorkspaceMeta

MAX_PATCH_BYTES = 512 * 1024  # 512 KB hard ceiling


async def _run_git(
    cwd: Path, *args: str, input_bytes: bytes | None = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate(input=input_bytes)
    code = proc.returncode if proc.returncode is not None else 1
    return code, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")


class GitWorkspacePatcher:
    """Captures uncommitted + untracked text changes and reconstructs them in a fresh worktree."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        worktree_target_dir: Path | str | None = None,
        max_patch_bytes: int = MAX_PATCH_BYTES,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.worktree_target_dir = (
            Path(worktree_target_dir).resolve() if worktree_target_dir else None
        )
        self.max_patch_bytes = max_patch_bytes
        self.last_restored_worktree: Path | None = None

    async def capture_patch(self) -> tuple[str, WorkspaceMeta]:
        """Run `git add -N .` then `git diff HEAD`, excluding binary files and capping at 512 KB."""
        code, _, err = await _run_git(self.workspace_root, "add", "-N", ".")
        if code != 0:
            raise RunLatchError(f"git add -N . failed: {err.strip()}")

        code, numstat_out, err = await _run_git(self.workspace_root, "diff", "--numstat", "HEAD")
        if code != 0:
            raise RunLatchError(f"git diff --numstat HEAD failed: {err.strip()}")

        binary_excludes: list[str] = []
        for line in numstat_out.splitlines():
            if line.startswith("-\t-\t"):
                binary_file = line.split("\t", 2)[-1].strip()
                if binary_file:
                    binary_excludes.append(f":(exclude){binary_file}")

        diff_args = ["diff", "--no-ext-diff", "HEAD", "--", ".", *binary_excludes]
        code, patch, err = await _run_git(self.workspace_root, *diff_args)
        if code != 0:
            raise RunLatchError(f"git diff HEAD failed: {err.strip()}")

        patch_bytes = len(patch.encode("utf-8"))
        if patch_bytes > self.max_patch_bytes:
            raise PatchTooLargeError(
                f"Workspace patch size ({patch_bytes} bytes) exceeds "
                f"{self.max_patch_bytes} bytes limit."
            )

        _, sha_out, _ = await _run_git(self.workspace_root, "rev-parse", "HEAD")
        _, branch_out, _ = await _run_git(
            self.workspace_root, "rev-parse", "--abbrev-ref", "HEAD"
        )
        _, remote_out, _ = await _run_git(
            self.workspace_root, "config", "--get", "remote.origin.url"
        )

        meta = WorkspaceMeta(
            repo_url=remote_out.strip(),
            base_commit_sha=sha_out.strip(),
            branch_name=branch_out.strip(),
            workspace_root=str(self.workspace_root),
        )
        return patch, meta

    async def _ensure_base_repo(self, meta: WorkspaceMeta) -> Path:
        """Return an existing local git repo root, or clone meta.repo_url on a remote worker."""
        if (self.workspace_root / ".git").exists():
            return self.workspace_root
        if meta.workspace_root and (Path(meta.workspace_root) / ".git").exists():
            return Path(meta.workspace_root).resolve()
        if not meta.repo_url:
            raise RunLatchError(
                f"Workspace '{self.workspace_root}' does not exist locally and "
                "WorkspaceMeta.repo_url is empty."
            )

        clone_dir = Path(tempfile.mkdtemp(prefix="runlatch_base_repo_")).resolve()
        code, _, err = await _run_git(
            clone_dir.parent, "clone", "--no-checkout", meta.repo_url, str(clone_dir)
        )
        if code != 0:
            raise RunLatchError(f"git clone {meta.repo_url} failed: {err.strip()}")
        return clone_dir

    async def apply_patch_in_worktree(
        self, patch: str, meta: WorkspaceMeta, target_dir: Path | None = None
    ) -> Path:
        """Create a fresh detached git worktree at base_commit_sha and apply patch with --3way."""
        base_repo = await self._ensure_base_repo(meta)
        dest = (
            Path(target_dir).resolve()
            if target_dir is not None
            else self.worktree_target_dir
            or Path(tempfile.mkdtemp(prefix="runlatch_wt_")).resolve()
        )
        if dest.exists() and any(dest.iterdir()):
            await _run_git(base_repo, "worktree", "remove", "--force", str(dest))

        commit_ref = meta.base_commit_sha or "HEAD"
        code, _, err = await _run_git(
            base_repo,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(dest),
            commit_ref,
        )
        if code != 0:
            raise RunLatchError(f"git worktree add failed: {err.strip()}")

        if patch.strip():
            patch_bytes = patch.encode("utf-8")
            if not patch_bytes.endswith(b"\n"):
                patch_bytes += b"\n"
            code, _, err = await _run_git(dest, "apply", "--3way", "-", input_bytes=patch_bytes)
            if code != 0:
                raise RunLatchError(f"git apply --3way failed in worktree: {err.strip()}")

        self.last_restored_worktree = dest
        return dest
