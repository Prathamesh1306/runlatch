"""Issue-to-PR Autonomous Patcher with Stateless HITL Gate (`issue-to-pr`).

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
    RegisteredTool,
    SecretRedactorProtocol,
)
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import LLMResponse, RiskTier, ToolCall
from runlatch.core.workflow import Step, Workflow
from runlatch.toolkits.filesystem import FileToolkit
from runlatch.toolkits.git import GitToolkit
from runlatch.tools.registry import FunctionTool


def issue_to_pr_demo_responses(issue_number: int = 108) -> list[LLMResponse]:
    """Deterministic responses for `runlatch run issue-to-pr --demo`."""
    return [
        # Step 1 (plan): Read workspace files and produce plan
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="plan_call_1",
                    name="list_files",
                    arguments={"directory": ".", "max_entries": 15},
                )
            ],
            input_tokens=110,
            output_tokens=25,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                f"Implementation Plan for Issue #{issue_number}: "
                "verified changes and tests pass."
            ),
            tool_calls=[],
            input_tokens=160,
            output_tokens=30,
            cost_usd=0.0,
            model="demo",
            latency_ms=2,
            finish_reason="stop",
        ),
        # Step 2 (publish): Request RiskTier.EXTERNAL tool -> triggers HITL pause!
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="pub_call_1",
                    name="github_create_pr",
                    arguments={
                        "title": f"fix: resolve issue #{issue_number}",
                        "body": f"Automated fix for issue #{issue_number} verified by runlatch.",
                        "head": f"runlatch/issue-{issue_number}",
                        "base": "main",
                    },
                )
            ],
            input_tokens=190,
            output_tokens=40,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        # Step 2 (publish - after human approves and resumes):
        LLMResponse(
            content=(
                f"Pull Request opened for issue #{issue_number}: "
                "https://github.com/demo/repo/pull/109"
            ),
            tool_calls=[],
            input_tokens=220,
            output_tokens=30,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="stop",
        ),
    ]


def _local_create_pr_tool() -> RegisteredTool:
    async def github_create_pr(
        title: str, body: str, head: str, base: str = "main"
    ) -> dict[str, object]:
        """Open a GitHub Pull Request (RiskTier.EXTERNAL, requires human approval)."""
        return {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "html_url": "https://github.com/demo/repo/pull/109",
            "status": "created",
        }

    return FunctionTool(
        github_create_pr,
        name="github_create_pr",
        risk=RiskTier.EXTERNAL,
        requires_approval=True,
    )


def build_issue_to_pr_workflow(
    provider: LLMProvider,
    *,
    workspace_root: Path | str = ".",
    max_cost_usd: float = 0.50,
    store: CheckpointStore | None = None,
    policy: PolicyEvaluator | None = None,
    redactor: SecretRedactorProtocol | None = None,
    publish_tools: list[RegisteredTool] | None = None,
) -> Workflow:
    """Construct the multi-step `issue-to-pr` Workflow with stateless HITL pause/resume."""
    root = Path(workspace_root).resolve()
    active_policy = policy or PolicyEngine(auto_approve_max_tier=RiskTier.WRITE)
    active_redactor = redactor or SecretRedactor()

    file_tk = FileToolkit(root)
    git_tk = GitToolkit(root)
    patcher = git_tk.patcher if (root / ".git").exists() else None

    planner = Agent(
        name="issue-planner",
        provider=provider,
        tools=file_tk.tools(),
        system_prompt="You are a Staff Engineer planning a minimal fix for a GitHub issue.",
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    pub_tool_list = publish_tools if publish_tools is not None else [_local_create_pr_tool()]
    publisher = Agent(
        name="pr-publisher",
        provider=provider,
        tools=pub_tool_list,
        system_prompt=(
            "You publish verified changes by opening a Pull Request via github_create_pr."
        ),
        policy=active_policy,
        redactor=active_redactor,
        patcher=patcher,
        store=store,
    )

    return (
        Workflow(name="issue-to-pr", max_cost_usd=max_cost_usd, store=store)
        .add_step(
            Step(
                name="plan",
                agent=planner,
                prompt="Create an implementation plan for issue #{issue_number}.",
                output_key="plan_output",
            )
        )
        .add_step(
            Step(
                name="publish",
                agent=publisher,
                prompt=(
                    "Publish the fix for issue #{issue_number} based on plan:\n{plan_output}\n"
                    "Call github_create_pr."
                ),
                output_key="publish_output",
            )
        )
    )
