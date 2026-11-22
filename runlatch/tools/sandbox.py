"""Workspace path containment, symlink escape guard, and denied-glob enforcement.

Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from runlatch.core.exceptions import SandboxViolationError

DEFAULT_DENIED_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
    "id_rsa*",
    "**/id_rsa*",
    ".git/config",
    "**/.git/config",
)


def _matches_denied_glob(rel_posix: str, globs: Sequence[str]) -> str | None:
    path_obj = PurePosixPath(rel_posix)
    basename = path_obj.name
    for pattern in globs:
        clean_pat = pattern.removeprefix("**/")
        if (
            fnmatch.fnmatch(rel_posix, pattern)
            or fnmatch.fnmatch(rel_posix, clean_pat)
            or fnmatch.fnmatch(basename, clean_pat)
            or path_obj.match(pattern)
        ):
            return pattern
    return None


def resolve_safe_path(
    workspace_root: Path | str,
    target_path: Path | str,
    *,
    denied_globs: Sequence[str] = DEFAULT_DENIED_GLOBS,
) -> Path:
    """Resolve target_path inside workspace_root, blocking traversal, symlinks, and denied globs."""
    raw_target = str(target_path).strip()
    if not raw_target or "\x00" in raw_target:
        raise SandboxViolationError("Invalid empty or null-byte path.")

    root_resolved = Path(workspace_root).resolve()
    candidate = Path(raw_target)

    # Check raw relative components for '..' before symlink resolution
    if not candidate.is_absolute():
        if ".." in candidate.parts:
            raise SandboxViolationError(
                f"Path traversal ('..') is forbidden: '{raw_target}'."
            )
        joined = root_resolved / candidate
    else:
        joined = candidate

    resolved = joined.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise SandboxViolationError(
            f"Path '{raw_target}' escapes workspace root '{root_resolved}'."
        )

    rel_posix = resolved.relative_to(root_resolved).as_posix()
    if rel_posix != ".":
        matched_glob = _matches_denied_glob(rel_posix, denied_globs)
        if matched_glob is not None:
            raise SandboxViolationError(
                f"Access to '{rel_posix}' is denied by policy glob '{matched_glob}'."
            )

    # Also check unresolved relative path in case a workspace symlink targets a denied filename
    if not candidate.is_absolute():
        raw_rel_posix = PurePosixPath(raw_target).as_posix()
        matched_raw = _matches_denied_glob(raw_rel_posix, denied_globs)
        if matched_raw is not None:
            raise SandboxViolationError(
                f"Access to '{raw_rel_posix}' is denied by policy glob '{matched_raw}'."
            )

    return resolved
