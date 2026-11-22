"""Slack Block Kit webhook notification provider."""

from __future__ import annotations

from typing import Any

import httpx

from runlatch.core.types import DeliveryReceipt, NotificationEvent


class SlackNotificationProvider:
    """Dispatches Slack Block Kit messages with read-only approval review links."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "slack"

    @staticmethod
    def build_payload(event: NotificationEvent) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": event.title[:150]},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Workflow:* `{event.workflow_name}` | *Run:* `{event.run_id}`\n"
                        f"{event.summary}"
                    ),
                },
            },
        ]
        review_url = event.action_urls.get("review")
        if review_url:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review & Decide (HITL)"},
                            "style": "primary",
                            "url": review_url,
                        }
                    ],
                }
            )
        return {"text": f"{event.title}: {event.summary}", "blocks": blocks}

    async def send(self, event: NotificationEvent, target: str) -> DeliveryReceipt:
        payload = self.build_payload(event)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.post(target, json=payload)
            if resp.status_code >= 400:
                return DeliveryReceipt(
                    channel=self.name,
                    target=target,
                    status="FAILED",
                    error=f"Slack HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="SENT",
                provider_message_id=resp.headers.get("x-slack-req-id", "slack-ok"),
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
