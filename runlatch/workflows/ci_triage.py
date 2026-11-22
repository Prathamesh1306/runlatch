"""CI Failure Root-Cause Triage workflow (`ci-triage`).

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


def ci_triage_demo_responses(workflow_run_id: int = 501) -> list[LLMResponse]:
    """Deterministic multi-turn tool-calling responses for `runlatch run ci-triage --demo`."""
    return [
        # Step 1 (extract_logs): Inspect recent commits and workspace files
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="ci_call_1", name="git_log", arguments={"limit": 5}),
                ToolCall(
                    id="ci_call_2",
                    name="list_files",
                    arguments={"directory": ".", "max_entries": 20},
                ),
            ],
            input_tokens=130,
            output_tokens=30,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                f"CI Failure Context (Run #{workflow_run_id}):\n"
                "- Extracted recent commit log and test suite structure.\n"
                "-Isolated failure candidate in recent workspace changes."
            ),
            tool_calls=[],
            input_tokens=190,
            output_tokens=45,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="stop",
        ),
        # Step 2 (diagnose_failure): Inspect git diff and classify root cause
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="ci_call_3", name="git_diff", arguments={}),
            ],
            input_tokens=175,
            output_tokens=25,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                f"## CI Failure Root-Cause Diagnosis (Run #{workflow_run_id})\n\n"
                "- **Classification:** `code_regression`\n"
                "- **Confidence:** High\n"
                "- **Root Cause:** Commit changed signature without updating caller.\n"
                "- **Proposed Fix:** Apply `SEARCH/REPLACE` patch to align arguments."
            ),
            tool_calls=[],
            input_tokens=240,
            output_tokens=75,
            cost_usd=0.0,
            model="demo",
            latency_ms=4,
            finish_reason="stop",
        ),
    ]


def build_ci_triage_workflow(
    provider: LLMProvider,
    *,
    workspace_root: Path | str = ".",
    max_cost_usd: float = 0.20,
    store: CheckpointStore | None = None,
    policy: PolicyEvaluator | None = None,
    redactor: SecretRedactorProtocol | None = None,
) -> Workflow:
    """Construct the multi-step `ci-triage` Workflow."""
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

    extractor = Agent(
        name="ci-log-extractor",
        provider=provider,
        tools=read_tools,
        system_prompt=(
            "You extract failed CI job context and commit history using git_log and list_files."
        ),
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    investigator = Agent(
        name="ci-root-cause-investigator",
        provider=provider,
        tools=read_tools,
        system_prompt=(
            "You correlate CI failure logs against git_diff and classify the root cause as "
            "code_regression, flaky_test, dependency_issue, or infra_timeout."
        ),
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    return (
        Workflow(name="ci-triage", max_cost_usd=max_cost_usd, store=store)
        .add_step(
            Step(
                name="extract_logs",
                agent=extractor,
                prompt="Extract commit and file context for failed CI workflow run.",
                output_key="ci_log_context",
            )
        )
        .add_step(
            Step(
                name="diagnose_failure",
                agent=investigator,
                prompt=(
                    "Diagnose the CI failure using this context:\n{ci_log_context}\n\n"
                    "Inspect git_diff and produce a Root-Cause Diagnosis report."
                ),
                output_key="diagnosis_report",
            )
        )
    )
