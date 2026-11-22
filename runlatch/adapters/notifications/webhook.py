"""Generic HMAC-SHA256 signed outbound JSON webhook notification provider."""

from __future__ import annotations

import hashlib
import hmac

import httpx

from runlatch.core.types import DeliveryReceipt, NotificationEvent


class WebhookNotificationProvider:
    """Dispatches HMAC-signed JSON POST requests to custom webhook endpoints."""

    def __init__(
        self,
        *,
        signing_secret: str = "dev-webhook-signing-secret",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.signing_secret = signing_secret
        self._client = client

    @property
    def name(self) -> str:
        return "webhook"

    def sign_payload(self, body_bytes: bytes) -> str:
        digest = hmac.new(
            self.signing_secret.encode("utf-8"), body_bytes, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    async def send(self, event: NotificationEvent, target: str) -> DeliveryReceipt:
        body_bytes = event.model_dump_json().encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-RunLatch-Event": event.event_type,
            "X-RunLatch-Signature-256": self.sign_payload(body_bytes),
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.post(target, content=body_bytes, headers=headers)
            if resp.status_code >= 400:
                return DeliveryReceipt(
                    channel=self.name,
                    target=target,
                    status="FAILED",
                    error=f"Webhook HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="SENT",
                provider_message_id=f"webhook-{resp.status_code}",
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
