"""GitHub PR / Issue Markdown comment notification provider."""

from __future__ import annotations

import os

import httpx

from runlatch.core.types import DeliveryReceipt, NotificationEvent


class GitHubCommentNotificationProvider:
    """Posts structured Markdown notifications to a GitHub PR or Issue (`owner/repo#number`)."""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self._client = client

    @property
    def name(self) -> str:
        return "github"

    @staticmethod
    def format_markdown(event: NotificationEvent) -> str:
        lines = [
            f"### {event.title}",
            f"**Workflow:** `{event.workflow_name}` | **Run ID:** `{event.run_id}`",
            "",
            event.summary,
        ]
        review_url = event.action_urls.get("review")
        if review_url:
            lines.extend(["", f"[**Review & Confirm Approval in runlatch UI**]({review_url})"])
        return "\n".join(lines)

    async def send(self, event: NotificationEvent, target: str) -> DeliveryReceipt:
        """Send comment to target formatted as `owner/repo#123` or full GitHub comments URL."""
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        elif "#" in target:
            repo_part, num_part = target.split("#", 1)
            url = f"{self.base_url}/repos/{repo_part.strip('/')}/issues/{num_part.strip()}/comments"
        else:
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="FAILED",
                error=f"Invalid GitHub comment target '{target}' (expected 'owner/repo#number').",
            )

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.post(
                url, headers=headers, json={"body": self.format_markdown(event)}
            )
            if resp.status_code >= 400:
                return DeliveryReceipt(
                    channel=self.name,
                    target=target,
                    status="FAILED",
                    error=f"GitHub HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="SENT",
                provider_message_id=str(data.get("id", "gh-ok")),
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="FAILED",
                error=str(exc),
            )
        finally:
            if owns_client:
                await client.aclose()
