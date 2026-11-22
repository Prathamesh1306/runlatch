"""Dependency & Security Audit workflow (`dep-audit`).

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


def dep_audit_demo_responses() -> list[LLMResponse]:
    """Deterministic multi-turn tool-calling responses for `runlatch run dep-audit --demo`."""
    return [
        # Step 1 (scan_dependencies): Read pyproject.toml
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="dep_call_1",
                    name="read_file",
                    arguments={"path": "pyproject.toml", "start_line": 1, "end_line": 60},
                )
            ],
            input_tokens=120,
            output_tokens=25,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                "Manifest Scan Summary:\n"
                "- Core: pydantic, pydantic-settings, httpx, sqlalchemy, aiosqlite, typer, rich.\n"
                "- Server extras: fastapi, uvicorn, jinja2, asyncpg, alembic, celery, redis."
            ),
            tool_calls=[],
            input_tokens=185,
            output_tokens=45,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="stop",
        ),
        # Step 2 (summarize_findings): Search codebase imports to filter false positives
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="dep_call_2",
                    name="search_code",
                    arguments={"pattern": "import ", "directory": ".", "max_matches": 15},
                )
            ],
            input_tokens=170,
            output_tokens=25,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                "## Dependency & Security Audit Digest\n\n"
                "- **Critical / High CVEs:** 0 active reachable vulnerabilities\n"
                "- **Layer 1 Core Footprint:** 7 direct packages (verified bounded ranges)\n"
                "- **Call-Site Reachability:** External adapters isolated behind protocols."
            ),
            tool_calls=[],
            input_tokens=230,
            output_tokens=70,
            cost_usd=0.0,
            model="demo",
            latency_ms=3,
            finish_reason="stop",
        ),
    ]


def build_dep_audit_workflow(
    provider: LLMProvider,
    *,
    workspace_root: Path | str = ".",
    max_cost_usd: float = 0.15,
    store: CheckpointStore | None = None,
    policy: PolicyEvaluator | None = None,
    redactor: SecretRedactorProtocol | None = None,
) -> Workflow:
    """Construct the multi-step `dep-audit` Workflow."""
    root = Path(workspace_root).resolve()
    active_policy = policy or PolicyEngine(auto_approve_max_tier=RiskTier.READ)
    active_redactor = redactor or SecretRedactor()

    file_tk = FileToolkit(root)
    read_tools = [t for t in file_tk.tools() if t.spec.risk_tier == RiskTier.READ]

    scanner = Agent(
        name="dependency-manifest-scanner",
        provider=provider,
        tools=read_tools,
        system_prompt="You inspect dependency manifests (pyproject.toml) using read_file.",
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    auditor = Agent(
        name="reachability-security-auditor",
        provider=provider,
        tools=read_tools,
        system_prompt=(
            "You verify dependency call-site reachability using search_code and produce a "
            "concise Dependency & Security Audit Digest."
        ),
        policy=active_policy,
        redactor=active_redactor,
        store=store,
    )

    return (
        Workflow(name="dep-audit", max_cost_usd=max_cost_usd, store=store)
        .add_step(
            Step(
                name="scan_dependencies",
                agent=scanner,
                prompt="Read pyproject.toml and list declared project dependencies.",
                output_key="manifest_summary",
            )
        )
        .add_step(
            Step(
                name="summarize_findings",
                agent=auditor,
                prompt=(
                    "Verify import reachability for these dependencies:\n{manifest_summary}\n\n"
                    "Use search_code and output the final Dependency & Security Audit Digest."
                ),
                output_key="audit_digest",
            )
        )
    )
