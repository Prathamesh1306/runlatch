"""Unit tests for DockerSandboxExecutor CLI arguments and all 5 NotificationProviders."""

from __future__ import annotations

from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from runlatch.adapters.executors.docker_exec import DockerSandboxExecutor
from runlatch.adapters.notifications import (
    DiscordNotificationProvider,
    GitHubCommentNotificationProvider,
    NotificationRouter,
    SlackNotificationProvider,
    SMTPEmailNotificationProvider,
    WebhookNotificationProvider,
)
from runlatch.core.exceptions import SandboxViolationError
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import NotificationEvent


@pytest.mark.asyncio
async def test_docker_sandbox_executor_builds_hardened_flags_and_blocks_interpreters(
    tmp_path: Path,
) -> None:
    captured_argv: list[str] = []

    async def fake_runner(
        argv: Sequence[str], cwd: Path, timeout_s: float
    ) -> tuple[int, str, str, bool]:
        del cwd, timeout_s
        captured_argv.extend(argv)
        return 0, "4 passed in 0.12s", "", False

    # Rejects raw python in allowlist
    with pytest.raises(SandboxViolationError):
        DockerSandboxExecutor(allowed_binaries=["python"])

    executor = DockerSandboxExecutor(
        image="python:3.12-slim",
        allowed_binaries=["pytest", "ruff"],
        memory_limit="512m",
        cpus="1",
        runner=fake_runner,
    )

    # Rejects shell metacharacters
    with pytest.raises(SandboxViolationError):
        await executor.run_command(["pytest", "-q; rm -rf /"], cwd=tmp_path)

    res = await executor.run_command(
        ["pytest", "-q"],
        cwd=tmp_path,
        env_overrides={"PYTEST_ADDOPTS": "-q"},
    )
    assert res.exit_code == 0
    assert "--rm" in captured_argv
    assert "--network=none" in captured_argv
    assert "--cap-drop=ALL" in captured_argv
    assert "--read-only" in captured_argv
    assert "--memory=512m" in captured_argv
    assert "--cpus=1" in captured_argv
    assert "python:3.12-slim" in captured_argv
    assert captured_argv[-2:] == ["pytest", "-q"]


@pytest.mark.asyncio
async def test_notification_router_and_all_five_providers() -> None:
    requests_seen: list[tuple[str, dict[str, str], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            (str(request.url), dict(request.headers), request.content.decode("utf-8"))
        )
        return httpx.Response(
            200,
            headers={"x-slack-req-id": "req-1"},
            json={"id": 555},
        )

    sent_emails: list[EmailMessage] = []

    def fake_smtp(msg: EmailMessage) -> str:
        sent_emails.append(msg)
        return "smtp-msg-1"

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        router = NotificationRouter(
            providers=[
                SlackNotificationProvider(client=client),
                DiscordNotificationProvider(client=client),
                GitHubCommentNotificationProvider(token="ghp_test", client=client),
                WebhookNotificationProvider(
                    signing_secret="wh-secret-key", client=client
                ),
                SMTPEmailNotificationProvider(transport_fn=fake_smtp),
            ],
            public_base_url="https://runlatch.example.com",
            signing_secret="approval-sign-secret",
            redactor=SecretRedactor(),
        )

        approval_id = uuid4()
        event = NotificationEvent(
            event_type="approval.requested",
            run_id=uuid4(),
            workflow_name="issue-to-pr",
            title="Approval needed for PR",
            summary="Diff ready (leaked key sk-proj-1234567890abcdefghijklmno)",
        )

        # 1. Slack
        r_slack = await router.send(
            event,
            channel="slack",
            target="https://hooks.slack.com/services/T0/B0/X0",
            approval_id=approval_id,
        )
        assert r_slack.status == "SENT"
        assert "/ui/approvals/" in requests_seen[-1][2]
        assert "[REDACTED_OPENAI_KEY]" in requests_seen[-1][2]

        # 2. Discord
        r_disc = await router.send(
            event,
            channel="discord",
            target="https://discord.com/api/webhooks/1/2",
            approval_id=approval_id,
        )
        assert r_disc.status == "SENT"

        # 3. GitHub Comment
        r_gh = await router.send(
            event,
            channel="github",
            target="Prathamesh1306/runlatch#42",
            approval_id=approval_id,
        )
        assert r_gh.status == "SENT"
        assert r_gh.provider_message_id == "555"

        # 4. Signed Webhook
        r_wh = await router.send(
            event,
            channel="webhook",
            target="https://example.com/hook",
            approval_id=approval_id,
        )
        assert r_wh.status == "SENT"
        assert requests_seen[-1][1].get("x-runlatch-signature-256", "").startswith(
            "sha256="
        )

        # 5. SMTP Email
        r_em = await router.send(
            event,
            channel="email",
            target="dev@runlatch.example.com",
            approval_id=approval_id,
        )
        assert r_em.status == "SENT"
        assert len(sent_emails) == 1
        assert "/ui/approvals/" in sent_emails[0].as_string()


def test_default_notification_router_smtp_wiring_and_shorthand_targets() -> None:
    from runlatch.config import RunLatchSettings
    from runlatch.runtime.workers.notification_tasks import (
        build_default_notification_router,
        parse_channel_target,
    )

    settings = RunLatchSettings(
        smtp_host="smtp.runlatch.internal",
        smtp_port=2525,
        smtp_username="mailer",
        smtp_password="super-secret-smtp-password-123",
        smtp_from="alerts@runlatch.internal",
        smtp_use_tls=False,
        slack_webhook_url="https://hooks.slack.com/services/T_DEFAULT/B_DEFAULT/SECRET99",
        discord_webhook_url="https://discord.com/api/webhooks/999/DISCORD_SECRET88",
    )

    # Secrets must be included in secret_values() for dynamic redaction
    secrets = settings.secret_values()
    assert "super-secret-smtp-password-123" in secrets
    assert "https://hooks.slack.com/services/T_DEFAULT/B_DEFAULT/SECRET99" in secrets
    assert "https://discord.com/api/webhooks/999/DISCORD_SECRET88" in secrets

    router = build_default_notification_router(settings)
    email_prov = router.get_provider("email")
    assert isinstance(email_prov, SMTPEmailNotificationProvider)
    assert email_prov.smtp_host == "smtp.runlatch.internal"
    assert email_prov.smtp_port == 2525
    assert email_prov.username == "mailer"
    assert email_prov.password == "super-secret-smtp-password-123"
    assert email_prov.sender_email == "alerts@runlatch.internal"
    assert email_prov.use_tls is False

    # Shorthand slack:default and discord:default resolve from settings
    ch_s, tgt_s = parse_channel_target("slack:default", settings)
    assert ch_s == "slack"
    assert tgt_s == "https://hooks.slack.com/services/T_DEFAULT/B_DEFAULT/SECRET99"

    ch_d, tgt_d = parse_channel_target("discord:default", settings)
    assert ch_d == "discord"
    assert tgt_d == "https://discord.com/api/webhooks/999/DISCORD_SECRET88"
