"""Discord Rich Embed webhook notification provider."""

from __future__ import annotations

from typing import Any

import httpx

from runlatch.core.types import DeliveryReceipt, NotificationEvent


class DiscordNotificationProvider:
    """Dispatches Discord rich embed notifications."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "discord"

    @staticmethod
    def build_payload(event: NotificationEvent) -> dict[str, Any]:
        description = event.summary
        review_url = event.action_urls.get("review")
        if review_url:
            description += f"\n\n[**Review & Approve/Reject (HITL)**]({review_url})"
        return {
            "content": event.title,
            "embeds": [
                {
                    "title": event.title[:256],
                    "description": description[:4000],
                    "fields": [
                        {"name": "Workflow", "value": event.workflow_name, "inline": True},
                        {"name": "Run ID", "value": str(event.run_id), "inline": True},
                        {"name": "Event", "value": event.event_type, "inline": True},
                    ],
                }
            ],
        }

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
                    error=f"Discord HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="SENT",
                provider_message_id="discord-ok",
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
