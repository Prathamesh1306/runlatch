"""FileToolkit with path-jailed inspection and deterministic SEARCH/REPLACE editing.

Imports ONLY Python stdlib, runlatch.core.*, and runlatch.tools.*.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from runlatch.core.exceptions import RunLatchError
from runlatch.core.protocols import RegisteredTool
from runlatch.core.types import RiskTier
from runlatch.tools.registry import FunctionTool
from runlatch.tools.sandbox import DEFAULT_DENIED_GLOBS, resolve_safe_path


def apply_search_replace(content: str, search: str, replace: str, *, file_label: str = "") -> str:
    """Apply a deterministic SEARCH/REPLACE block with whitespace-normalized fallback."""
    if not search:
        raise RunLatchError("SEARCH block must not be empty.")

    # 1. Exact substring match
    exact_count = content.count(search)
    if exact_count == 1:
        return content.replace(search, replace, 1)
    if exact_count > 1:
        raise RunLatchError(
            f"Ambiguous SEARCH block matched {exact_count} locations in '{file_label}'; "
            "include more surrounding context lines."
        )

    # 2. Whitespace-normalized line-by-line fallback
    content_lines = content.splitlines(keepends=True)
    search_lines = [line.strip() for line in search.splitlines()]
    if not search_lines:
        raise RunLatchError(f"SEARCH block not found in '{file_label}'.")

    window = len(search_lines)
    matches: list[int] = []
    for idx in range(len(content_lines) - window + 1):
        candidate = [line.strip() for line in content_lines[idx : idx + window]]
        if candidate == search_lines:
            matches.append(idx)

    if len(matches) == 1:
        start_idx = matches[0]
        trailing_newline = "\n" if content_lines[start_idx + window - 1].endswith("\n") else ""
        replacement_text = replace
        if trailing_newline and replacement_text and not replacement_text.endswith("\n"):
            replacement_text += "\n"
        prefix = "".join(content_lines[:start_idx])
        suffix = "".join(content_lines[start_idx + window :])
        return f"{prefix}{replacement_text}{suffix}"

    if len(matches) > 1:
        raise RunLatchError(
            f"Ambiguous whitespace-normalized SEARCH block matched {len(matches)} "
            f"locations in '{file_label}'."
        )

    raise RunLatchError(f"SEARCH block not found in '{file_label}'.")


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


class FileToolkit:
    """Path-jailed repository filesystem toolkit."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        denied_globs: Sequence[str] = DEFAULT_DENIED_GLOBS,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.denied_globs = tuple(denied_globs)

    def _safe_path(self, rel_path: str) -> Path:
        return resolve_safe_path(
            self.workspace_root, rel_path, denied_globs=self.denied_globs
        )

    def list_files(self, directory: str = ".", max_entries: int = 200) -> str:
        """List non-hidden files inside a directory relative to the workspace root."""
        base = self._safe_path(directory)
        if not base.is_dir():
            raise RunLatchError(f"Directory not found: '{directory}'")

        entries: list[str] = []
        for path in sorted(base.rglob("*")):
            if len(entries) >= max_entries:
                break
            if any(part.startswith(".") for part in path.relative_to(self.workspace_root).parts):
                continue
            if path.is_file():
                rel = path.relative_to(self.workspace_root).as_posix()
                try:
                    resolve_safe_path(
                        self.workspace_root, rel, denied_globs=self.denied_globs
                    )
                    entries.append(rel)
                except Exception:
                    continue
        return "\n".join(entries) if entries else "(no files found)"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 200) -> str:
        """Read numbered lines from a file inside the repository workspace."""
        target = self._safe_path(path)
        if not target.is_file():
            raise RunLatchError(f"File not found: '{path}'")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        s_idx = max(start_line - 1, 0)
        e_idx = min(max(end_line, start_line), len(lines))
        numbered = [f"{i + 1}: {lines[i]}" for i in range(s_idx, e_idx)]
        return "\n".join(numbered)

    def search_code(self, pattern: str, directory: str = ".", max_matches: int = 50) -> str:
        """Search for a regex pattern across workspace files."""
        base = self._safe_path(directory)
        compiled = re.compile(pattern)
        hits: list[str] = []
        for path in sorted(base.rglob("*")):
            if len(hits) >= max_matches:
                break
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.workspace_root).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            rel = path.relative_to(self.workspace_root).as_posix()
            try:
                self._safe_path(rel)
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    hits.append(f"{rel}:{line_no}: {line.strip()}")
                    if len(hits) >= max_matches:
                        break
        return "\n".join(hits) if hits else "No matches found."

    def edit_file(
        self,
        path: str,
        search: str = "",
        replace: str = "",
        *,
        create_if_missing: bool = False,
    ) -> str:
        """Deterministically edit a file using a SEARCH/REPLACE block and atomic write."""
        target = self._safe_path(path)
        if not target.exists():
            if create_if_missing and not search:
                _atomic_write_text(target, replace)
                return f"Created '{path}' ({len(replace)} chars)."
            raise RunLatchError(f"Cannot edit non-existent file '{path}'.")

        original = target.read_text(encoding="utf-8")
        updated = apply_search_replace(original, search, replace, file_label=path)
        _atomic_write_text(target, updated)
        return f"Updated '{path}' via SEARCH/REPLACE ({len(updated)} chars)."

    def tools(self) -> list[RegisteredTool]:
        """Return RegisteredTool instances for this toolkit."""
        return [
            FunctionTool(self.list_files, name="list_files", risk=RiskTier.READ),
            FunctionTool(self.read_file, name="read_file", risk=RiskTier.READ),
            FunctionTool(self.search_code, name="search_code", risk=RiskTier.READ),
            FunctionTool(self.edit_file, name="edit_file", risk=RiskTier.WRITE),
        ]
