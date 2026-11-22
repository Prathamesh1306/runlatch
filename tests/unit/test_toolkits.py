"""Unit tests for FileToolkit (SEARCH/REPLACE), GitToolkit, and GitHubToolkit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from runlatch.core.exceptions import RunLatchError
from runlatch.toolkits.filesystem import FileToolkit
from runlatch.toolkits.git import GitToolkit
from runlatch.toolkits.github import GitHubToolkit


def test_file_toolkit_search_replace_exact_and_whitespace_fallback(tmp_path: Path) -> None:
    src = tmp_path / "service.py"
    src.write_text(
        "def compute(x: int) -> int:\n    val = x + 1\n    return val\n",
        encoding="utf-8",
    )
    tk = FileToolkit(tmp_path)

    # 1. Exact SEARCH/REPLACE match
    tk.edit_file(
        "service.py",
        search="    val = x + 1\n    return val",
        replace="    return x * 2",
    )
    assert "return x * 2" in src.read_text(encoding="utf-8")

    # 2. Whitespace-normalized fallback (different leading indentation in SEARCH)
    tk.edit_file(
        "service.py",
        search="def compute(x: int) -> int:\nreturn x * 2",
        replace="def compute(x: int) -> int:\n    return x * 10",
    )
    assert "return x * 10" in src.read_text(encoding="utf-8")

    # 3. Missing SEARCH block raises RunLatchError without corrupting file
    with pytest.raises(RunLatchError):
        tk.edit_file("service.py", search="non_existent_code()", replace="noop")
    assert "return x * 10" in src.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_toolkit_diff_includes_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dev@runlatch.dev"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True
    )

    # Add a brand-new untracked file and an unrelated binary file
    (tmp_path / "new_helper.py").write_text(
        "def help_me() -> int:\n    return 42\n", encoding="utf-8"
    )
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\xff")

    gtk = GitToolkit(tmp_path)
    diff = await gtk.git_diff()
    assert "new_helper.py" in diff
    assert "+def help_me() -> int:" in diff
    # Binary file must be cleanly excluded without raising
    assert "image.png" not in diff

    # Applying the captured patch into a fresh git worktree succeeds cleanly
    patch, meta = await gtk.capture_patch()
    wt_dir = tmp_path.parent / f"{tmp_path.name}_wt"
    restored = await gtk.apply_patch_in_worktree(patch, meta, target_dir=wt_dir)
    assert (restored / "new_helper.py").is_file()

    # Text patch exceeding max_patch_bytes raises PatchTooLargeError
    from runlatch.core.exceptions import PatchTooLargeError

    small_gtk = GitToolkit(tmp_path, max_patch_bytes=10)
    with pytest.raises(PatchTooLargeError):
        await small_gtk.git_diff()


@pytest.mark.asyncio
async def test_github_toolkit_mocked_endpoints() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/42"):
            if "diff" in request.headers.get("Accept", ""):
                return httpx.Response(200, text="diff --git a/a.py b/a.py\n+x = 2\n")
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "feat: add safety check",
                    "body": "PR description",
                    "state": "open",
                    "head": {"ref": "feat/safety"},
                    "base": {"ref": "main"},
                    "changed_files": 1,
                    "additions": 5,
                    "deletions": 1,
                },
            )
        return httpx.Response(201, json={"id": 777, "html_url": "https://github.com/o/r/pull/42#c1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gh = GitHubToolkit("owner/repo", token="ghp_test", client=client)
        diff = await gh.github_get_pr_diff(42)
        meta = await gh.github_get_pr_metadata(42)
        comment = await gh.github_post_pr_comment(42, "LGTM")

    assert "diff --git" in diff
    assert meta["title"] == "feat: add safety check"
    assert comment["id"] == 777
