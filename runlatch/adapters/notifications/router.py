"""Notification router with secret redaction and signed read-only `/ui/approvals/{id}` URL builder.

Imports ONLY Python stdlib and runlatch.core.*.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from uuid import UUID

from runlatch.core.exceptions import RunLatchError
from runlatch.core.protocols import NotificationProvider, SecretRedactorProtocol
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import DeliveryReceipt, NotificationEvent


def build_signed_review_url(
    base_url: str,
    approval_id: UUID | str,
    secret: str,
    *,
    ttl_minutes: int = 60,
    now_ts: int | None = None,
) -> str:
    """Build a read-only `GET /ui/approvals/{id}?sig=...&exp=...` review URL."""
    base = base_url.rstrip("/")
    current = now_ts if now_ts is not None else int(time.time())
    exp = current + (ttl_minutes * 60)
    msg = f"{approval_id}:{exp}".encode()
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{base}/ui/approvals/{approval_id}?sig={sig}&exp={exp}"


class NotificationRouter:
    """Routes NotificationEvents to registered providers with secret redaction and signed links."""

    def __init__(
        self,
        providers: Sequence[NotificationProvider] = (),
        *,
        public_base_url: str = "http://localhost:8000",
        signing_secret: str = "dev-approval-signing-secret-32bytes",
        approval_ttl_minutes: int = 60,
        redactor: SecretRedactorProtocol | None = None,
    ) -> None:
        self._providers: dict[str, NotificationProvider] = {p.name: p for p in providers}
        self.public_base_url = public_base_url.rstrip("/")
        self.signing_secret = signing_secret
        self.approval_ttl_minutes = approval_ttl_minutes
        self.redactor: SecretRedactorProtocol = redactor or SecretRedactor()

    def register(self, provider: NotificationProvider) -> None:
        self._providers[provider.name] = provider

    def get_provider(self, channel: str) -> NotificationProvider:
        provider = self._providers.get(channel)
        if provider is None:
            raise RunLatchError(f"Notification provider '{channel}' is not registered.")
        return provider

    def prepare_event(
        self,
        event: NotificationEvent,
        *,
        approval_id: UUID | str | None = None,
    ) -> NotificationEvent:
        """Scrub secrets from event text/metadata and attach signed review URL if applicable."""
        sanitized_meta = self.redactor.redact_obj(event.metadata)
        action_urls = dict(event.action_urls)
        resolved_approval_id = approval_id or event.metadata.get("approval_id")
        if resolved_approval_id and "review" not in action_urls:
            review_url = build_signed_review_url(
                self.public_base_url,
                resolved_approval_id,
                self.signing_secret,
                ttl_minutes=self.approval_ttl_minutes,
            )
            action_urls["review"] = review_url

        return NotificationEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            run_id=event.run_id,
            workflow_name=event.workflow_name,
            title=self.redactor.redact_text(event.title),
            summary=self.redactor.redact_text(event.summary),
            metadata=dict(sanitized_meta) if isinstance(sanitized_meta, Mapping) else {},
            action_urls=action_urls,
        )

    async def send(
        self,
        event: NotificationEvent,
        *,
        channel: str,
        target: str,
        approval_id: UUID | str | None = None,
    ) -> DeliveryReceipt:
        """Prepare and dispatch an event to a single `(channel, target)` destination."""
        provider = self.get_provider(channel)
        prepared = self.prepare_event(event, approval_id=approval_id)
        return await provider.send(prepared, target)
