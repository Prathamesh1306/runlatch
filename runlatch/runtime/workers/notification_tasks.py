"""Celery notification dispatcher task on isolated `agent.notifications` queue with 5x backoff."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from runlatch.adapters.notifications import (
    DiscordNotificationProvider,
    GitHubCommentNotificationProvider,
    NotificationRouter,
    SlackNotificationProvider,
    SMTPEmailNotificationProvider,
    WebhookNotificationProvider,
)
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import NotificationEvent
from runlatch.runtime.workers.celery_app import (
    QUEUE_NOTIFICATIONS,
    celery_app,
    run_sync_with_null_pool,
)

# 5x exponential backoff schedule (10s, 30s, 90s, 270s, 810s)
NOTIFICATION_BACKOFF_SECONDS: tuple[int, ...] = (10, 30, 90, 270, 810)


def build_default_notification_router(
    settings: RunLatchSettings,
    *,
    router: NotificationRouter | None = None,
) -> NotificationRouter:
    """Build a NotificationRouter wired with all 5 default channel providers and SMTP settings."""
    if router is not None:
        return router
    redactor = SecretRedactor(dynamic_secrets=settings.secret_values())
    return NotificationRouter(
        providers=[
            SlackNotificationProvider(),
            DiscordNotificationProvider(),
            GitHubCommentNotificationProvider(token=settings.github_token),
            WebhookNotificationProvider(signing_secret=settings.approval_signing_secret),
            SMTPEmailNotificationProvider(
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                sender_email=settings.smtp_from,
                username=settings.smtp_username or None,
                password=settings.smtp_password or None,
                use_tls=settings.smtp_use_tls,
            ),
        ],
        public_base_url=settings.public_base_url,
        signing_secret=settings.approval_signing_secret,
        approval_ttl_minutes=settings.approval_ttl_minutes,
        redactor=redactor,
    )


def parse_channel_target(
    spec: str, settings: RunLatchSettings | None = None
) -> tuple[str, str]:
    """Parse `'slack:default'` or `'github:owner/repo#42'` into `(channel, target)`."""
    if ":" not in spec:
        return "webhook", spec
    channel, raw_target = spec.split(":", 1)
    clean_channel = channel.strip().lower()
    target = raw_target.strip()

    if settings is not None:
        if clean_channel == "slack" and (
            target in ("default", "") or target.startswith("#")
        ):
            if settings.slack_webhook_url:
                target = settings.slack_webhook_url
        elif clean_channel == "discord" and (
            target in ("default", "") or target.startswith("#")
        ):
            if settings.discord_webhook_url:
                target = settings.discord_webhook_url

    return clean_channel, target


async def deliver_notification_async(
    store: SQLAlchemyCheckpointStore,
    settings: RunLatchSettings,
    *,
    event_dict: dict[str, Any],
    channel_spec: str,
    approval_id: str | None = None,
    delivery_id: UUID | None = None,
    attempt: int = 1,
    max_attempts: int = 5,
    router: NotificationRouter | None = None,
) -> dict[str, Any]:
    """Execute one notification delivery attempt and record audit state in DB."""
    event = NotificationEvent.model_validate(event_dict)
    channel, target = parse_channel_target(channel_spec, settings=settings)
    active_router = build_default_notification_router(settings, router=router)

    active_delivery_id = delivery_id or await store.create_notification_delivery(
        run_id=event.run_id,
        event_type=event.event_type,
        channel=channel,
        target=target,
        status="PENDING",
    )

    receipt = await active_router.send(
        event,
        channel=channel,
        target=target,
        approval_id=approval_id,
    )
    if receipt.status == "SENT":
        await store.update_notification_delivery(
            active_delivery_id,
            status="SENT",
            attempt_count=attempt,
            provider_message_id=receipt.provider_message_id,
        )
        return {
            "delivery_id": str(active_delivery_id),
            "status": "SENT",
            "channel": channel,
            "target": target,
            "attempt": attempt,
        }

    final_status = "DEAD_LETTER" if attempt >= max_attempts else "FAILED"
    await store.update_notification_delivery(
        active_delivery_id,
        status=final_status,
        attempt_count=attempt,
        last_error=receipt.error,
    )
    return {
        "delivery_id": str(active_delivery_id),
        "status": final_status,
        "channel": channel,
        "target": target,
        "attempt": attempt,
        "error": receipt.error,
    }


@celery_app.task(  # type: ignore[untyped-decorator]
    name="runlatch.workers.dispatch_notification_task",
    bind=True,
    queue=QUEUE_NOTIFICATIONS,
    max_retries=5,
    soft_time_limit=20,
    time_limit=30,
)
def dispatch_notification_task(
    self: Any,
    event_dict: dict[str, Any],
    channel_spec: str,
    approval_id: str | None = None,
    delivery_id_str: str | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Celery task dispatching notifications on `agent.notifications` with 5x backoff."""
    attempt = int(getattr(self.request, "retries", 0)) + 1
    max_attempts = int(getattr(self, "max_retries", 5)) + 1
    parsed_delivery_id = UUID(delivery_id_str) if delivery_id_str else None

    async def _op(
        store: SQLAlchemyCheckpointStore, settings: RunLatchSettings
    ) -> dict[str, Any]:
        return await deliver_notification_async(
            store,
            settings,
            event_dict=event_dict,
            channel_spec=channel_spec,
            approval_id=approval_id,
            delivery_id=parsed_delivery_id,
            attempt=attempt,
            max_attempts=max_attempts,
        )

    res = run_sync_with_null_pool(_op, database_url=database_url)
    if res["status"] == "FAILED" and attempt <= int(getattr(self, "max_retries", 5)):
        idx = min(attempt - 1, len(NOTIFICATION_BACKOFF_SECONDS) - 1)
        countdown = NOTIFICATION_BACKOFF_SECONDS[idx]
        raise self.retry(
            countdown=countdown,
            kwargs={"delivery_id_str": res["delivery_id"]},
        )
    return res
