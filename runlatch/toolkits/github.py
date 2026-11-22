"""Async httpx GitHub REST API toolkit.

Imports ONLY Python stdlib, httpx, runlatch.core.*, and runlatch.tools.*.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from runlatch.core.exceptions import RunLatchError
from runlatch.core.protocols import RegisteredTool
from runlatch.core.types import RiskTier
from runlatch.tools.registry import FunctionTool


class GitHubToolkit:
    """GitHub REST API tools for PR diffs, issues, comments, PR creation, and workflow logs."""

    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repo = repo.strip("/")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self._client = client

    def _headers(self, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/repos/{self.repo}/{path.lstrip('/')}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await client.request(
                method,
                url,
                headers=self._headers(accept=accept),
                json=json_body,
            )
            if resp.status_code >= 400:
                raise RunLatchError(
                    f"GitHub API {method} {path} failed with HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
            return resp
        finally:
            if owns_client:
                await client.aclose()

    async def github_get_pr_diff(self, pr_number: int) -> str:
        """Fetch the unified diff of a GitHub Pull Request."""
        resp = await self._request(
            "GET",
            f"pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        return resp.text

    async def github_get_pr_metadata(self, pr_number: int) -> dict[str, Any]:
        """Fetch title, author, branch names, and changed-file counts for a Pull Request."""
        resp = await self._request("GET", f"pulls/{pr_number}")
        data = resp.json()
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "body": data.get("body") or "",
            "state": data.get("state"),
            "head": (data.get("head") or {}).get("ref"),
            "base": (data.get("base") or {}).get("ref"),
            "changed_files": data.get("changed_files"),
            "additions": data.get("additions"),
            "deletions": data.get("deletions"),
        }

    async def github_get_issue(self, issue_number: int) -> dict[str, Any]:
        """Fetch title, body, and labels of a GitHub Issue."""
        resp = await self._request("GET", f"issues/{issue_number}")
        data = resp.json()
        labels = [
            lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
            for lbl in (data.get("labels") or [])
        ]
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "body": data.get("body") or "",
            "state": data.get("state"),
            "labels": labels,
        }

    async def github_get_workflow_logs(self, run_id: int) -> str:
        """Fetch failed job step summary for a GitHub Actions workflow run."""
        resp = await self._request("GET", f"actions/runs/{run_id}/jobs")
        data = resp.json()
        lines: list[str] = []
        for job in data.get("jobs") or []:
            lines.append(f"Job: {job.get('name')} (conclusion={job.get('conclusion')})")
            for step in job.get("steps") or []:
                if step.get("conclusion") not in ("success", "skipped", None):
                    lines.append(
                        f"  FAILED Step #{step.get('number')}: {step.get('name')} "
                        f"({step.get('conclusion')})"
                    )
        return "\n".join(lines) if lines else "No failed jobs found."

    async def github_post_pr_comment(self, pr_number: int, body: str) -> dict[str, Any]:
        """Post a comment on a GitHub Pull Request or Issue."""
        resp = await self._request(
            "POST",
            f"issues/{pr_number}/comments",
            json_body={"body": body},
        )
        data = resp.json()
        return {"id": data.get("id"), "html_url": data.get("html_url")}

    async def github_create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> dict[str, Any]:
        """Open a GitHub Pull Request (RiskTier.EXTERNAL, requires human approval)."""
        resp = await self._request(
            "POST",
            "pulls",
            json_body={"title": title, "body": body, "head": head, "base": base},
        )
        data = resp.json()
        return {
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
        }

    def tools(self) -> list[RegisteredTool]:
        return [
            FunctionTool(
                self.github_get_pr_diff, name="github_get_pr_diff", risk=RiskTier.READ
            ),
            FunctionTool(
                self.github_get_pr_metadata,
                name="github_get_pr_metadata",
                risk=RiskTier.READ,
            ),
            FunctionTool(
                self.github_get_issue, name="github_get_issue", risk=RiskTier.READ
            ),
            FunctionTool(
                self.github_get_workflow_logs,
                name="github_get_workflow_logs",
                risk=RiskTier.READ,
            ),
            FunctionTool(
                self.github_post_pr_comment,
                name="github_post_pr_comment",
                risk=RiskTier.EXTERNAL,
                requires_approval=True,
            ),
            FunctionTool(
                self.github_create_pr,
                name="github_create_pr",
                risk=RiskTier.EXTERNAL,
                requires_approval=True,
            ),
        ]
