"""Async SMTP email notification provider."""

from __future__ import annotations

import asyncio
import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from runlatch.core.types import DeliveryReceipt, NotificationEvent


class SMTPEmailNotificationProvider:
    """Sends HTML + plain-text email notifications via SMTP."""

    def __init__(
        self,
        *,
        smtp_host: str = "localhost",
        smtp_port: int = 587,
        sender_email: str = "noreply@runlatch.dev",
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        transport_fn: Callable[[EmailMessage], str] | None = None,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self._transport_fn = transport_fn

    @property
    def name(self) -> str:
        return "email"

    def build_message(self, event: NotificationEvent, target: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[runlatch] {event.title}"
        msg["From"] = self.sender_email
        msg["To"] = target

        review_url = event.action_urls.get("review")
        plain = (
            f"{event.title}\nWorkflow: {event.workflow_name}\n"
            f"Run: {event.run_id}\n\n{event.summary}"
        )
        if review_url:
            plain += f"\n\nReview & Confirm Approval: {review_url}"
        msg.set_content(plain)

        html_link = (
            f'<p><a href="{review_url}"><strong>Review &amp; Decide in runlatch UI</strong></a></p>'
            if review_url
            else ""
        )
        msg.add_alternative(
            f"<html><body><h3>{event.title}</h3>"
            f"<p><strong>Workflow:</strong> {event.workflow_name} | "
            f"<strong>Run:</strong> <code>{event.run_id}</code></p>"
            f"<p>{event.summary}</p>{html_link}</body></html>",
            subtype="html",
        )
        return msg

    def _send_sync(self, msg: EmailMessage) -> str:
        if self._transport_fn is not None:
            return self._transport_fn(msg)
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
            if self.use_tls:
                server.starttls()
            if self.username and self.password:
                server.login(self.username, self.password)
            server.send_message(msg)
        return "smtp-ok"

    async def send(self, event: NotificationEvent, target: str) -> DeliveryReceipt:
        msg = self.build_message(event, target)
        try:
            msg_id = await asyncio.to_thread(self._send_sync, msg)
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="SENT",
                provider_message_id=msg_id,
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel=self.name,
                target=target,
                status="FAILED",
                error=str(exc),
            )
