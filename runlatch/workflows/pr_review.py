"""Autonomous PR Review & Risk Analyzer workflow (`pr-review`).

Pure composition module: imports ONLY Python stdlib, runlatch.core.*, and runlatch.toolkits.*.
"""

from __future__ import annotations

from pathlib import Path

from runlatch.core.agent import Agent
from runlatch.core.policy import PolicyEngine
from runlatch.core.protocols import (
    CheckpointStore,
    LLMProvider,
    PolicyEvaluator,
    SecretRedactorProtocol,
)
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import LLMResponse, RiskTier, ToolCall
from runlatch.core.workflow import Step, Workflow
from runlatch.toolkits.filesystem import FileToolkit
from runlatch.toolkits.git import GitToolkit


def pr_review_demo_responses() -> list[LLMResponse]:
    """Deterministic multi-turn tool-calling responses for `runlatch run pr-review --demo`."""
    return [
        # Step 1 (gather_context) - Turn 1: Parallel READ tool calls
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="demo_call_1", name="git_status", arguments={}),
                ToolCall(
                    id="demo_call_2",
                    name="list_files",
                    arguments={"directory": ".", "max_entries": 25},
                ),
            ],
            input_tokens=140,
            output_tokens=35,
            cost_usd=0.0,
            model="demo",
            latency_ms=4,
            finish_reason="tool_calls",
        ),
        # Step 1 (gather_context) - Turn 2: Context synthesis
        LLMResponse(
            content=(
                "PR Context Summary:\n"
                "- Repository workspace inspected cleanly.\n"
                "- Hexagonal boundaries verified across `runlatch/core`, `runlatch/toolkits`, "
                "and `runlatch/adapters`.\n"
                "- Proceeding to deep diff and security review."
            ),
            tool_calls=[],
            input_tokens=210,
            output_tokens=55,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="stop",
        ),
        # Step 2 (security_and_code_review) - Turn 1: Inspect git diff
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="demo_call_3", name="git_diff", arguments={}),
            ],
            input_tokens=180,
            output_tokens=25,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        # Step 2 (security_and_code_review) - Turn 2: Final structured PR review report
        LLMResponse(
            content=(
                "## Autonomous PR Review Report\n\n"
                "### Verdict: APPROVED (0 Critical, 0 High, 1 Info)\n\n"
                "| Check | Status | Details |\n"
                "|---|---|---|\n"
                "| Hexagonal Import Boundaries | PASS | `runlatch/core/` pure domain |\n"
                "| Shell & Path Sandbox | PASS | `shell=False` allowlist & path guard |\n"
                "| Secret Hygiene | PASS | No hardcoded credentials or unredacted tokens |\n"
                "| Stateless HITL Readiness | PASS | `git add -N .` + `git diff HEAD` verified |\n"
            ),
            tool_calls=[],
            input_tokens=260,
            output_tokens=95,
            cost_usd=0.0,
            model="demo",
            latency_ms=4,
            finish_reason="stop",
        ),
    ]


def build_pr_review_workflow(
    provider: LLMProvider,
    *,
    workspace_root: Path | str = ".",
    max_cost_usd: float = 0.15,
    store: CheckpointStore | None = None,
    policy: PolicyEvaluator | None = None,
    redactor: SecretRedactorProtocol | None = None,
) -> Workflow:
    """Construct the multi-step `pr-review` Workflow."""
    root = Path(workspace_root).resolve()
    active_policy = policy or PolicyEngine(auto_approve_max_tier=RiskTier.READ)
    active_redactor = redactor or SecretRedactor()

    file_tk = FileToolkit(root)
    git_tk = GitToolkit(root)
    read_tools = [
        t
        for t in [*git_tk.tools(), *file_tk.tools()]
        if t.spec.risk_tier == RiskTier.READ
    ]

    gatherer = Agent(
        name="pr-context-gatherer",
        provider=provider,
        tools=read_tools,
        system_prompt=(
            "You are a Staff Engineer gathering repository and diff context for a PR review. "
            "Use git_status and list_files to inspect the repository state."
        ),
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    reviewer = Agent(
        name="pr-security-reviewer",
        provider=provider,
        tools=read_tools,
        system_prompt=(
            "You are a Security & Architecture Reviewer. Inspect the diff using git_diff and "
            "produce a concise Markdown PR Review Report covering bugs, security, and tests."
        ),
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    return (
        Workflow(name="pr-review", max_cost_usd=max_cost_usd, store=store)
        .add_step(
            Step(
                name="gather_context",
                agent=gatherer,
                prompt="Inspect workspace status and changed files for PR #{pr_number}.",
                output_key="context_summary",
            )
        )
        .add_step(
            Step(
                name="security_and_code_review",
                agent=reviewer,
                prompt=(
                    "Review PR #{pr_number} using this context:\n{context_summary}\n\n"
                    "Run git_diff and output the final Markdown review report."
                ),
                output_key="review_report",
            )
        )
    )
