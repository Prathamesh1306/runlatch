"""Typer + Rich CLI for `runlatch` (`init`, `run`, `runs`, `approvals`, `approve`, `reject`)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.config import RunLatchSettings
from runlatch.core.exceptions import RunLatchError
from runlatch.core.protocols import LLMProvider
from runlatch.core.types import ApprovalStatus, RunResult, RunStatus
from runlatch.core.workflow import Workflow
from runlatch.workflows.issue_to_pr import (
    build_issue_to_pr_workflow,
    issue_to_pr_demo_responses,
)
from runlatch.workflows.pr_review import (
    build_pr_review_workflow,
    pr_review_demo_responses,
)

app = typer.Typer(
    name="runlatch",
    help="runlatch — Durable, HITL-First Execution Runtime for Autonomous Developer Agents.",
    no_args_is_help=True,
)
runs_app = typer.Typer(help="List and inspect workflow runs.")
approvals_app = typer.Typer(help="List pending Human-in-the-Loop approval requests.")
app.add_typer(runs_app, name="runs")
app.add_typer(approvals_app, name="approvals")

console = Console()

DEFAULT_TOML_TEMPLATE = """[policy]
# Actions at or below this tier auto-execute; higher tiers pause for HITL approval
auto_approve_max_tier = "WRITE"
require_approval_tools = ["git_push", "github_create_pr"]
denied_tools = []

[policy.shell]
executor = "subprocess"
allowed_binaries = ["pytest", "ruff", "mypy", "git"]
timeout_seconds = 60

[policy.filesystem]
denied_globs = [".env*", "**/*.pem", "**/*.key", "**/.git/config"]

[policy.budget]
max_steps = 15
max_tokens = 80000
max_cost_usd = 0.50
max_duplicate_tool_calls = 2
"""


def _resolve_provider(
    *,
    workflow_name: str,
    demo: bool,
    provider_name: str | None,
    model: str | None,
    issue: int,
    settings: RunLatchSettings,
) -> LLMProvider:
    if demo:
        if workflow_name == "pr-review":
            return MockLLMProvider(pr_review_demo_responses(), model="demo")
        if workflow_name == "issue-to-pr":
            return MockLLMProvider(issue_to_pr_demo_responses(issue), model="demo")
        return MockLLMProvider(pr_review_demo_responses(), model="demo")

    chosen = (provider_name or settings.llm_provider).strip().lower()
    if chosen == "mock":
        return MockLLMProvider(pr_review_demo_responses(), model="mock")
    if chosen == "ollama":
        return OpenAICompatibleProvider.ollama(
            model=model or settings.llm_model,
            base_url=settings.llm_base_url,
        )
    if chosen == "groq":
        return OpenAICompatibleProvider.groq(
            api_key=settings.groq_api_key or settings.llm_api_key,
            model=model or "llama-3.3-70b-versatile",
        )
    if chosen in ("github", "github-models", "github_models"):
        return OpenAICompatibleProvider.github_models(
            token=settings.github_token or settings.llm_api_key,
            model=model or "gpt-4o-mini",
        )
    return OpenAICompatibleProvider(
        model=model or settings.llm_model,
        api_key=settings.openai_api_key or settings.llm_api_key,
        base_url=settings.llm_base_url,
    )


def _build_workflow(
    workflow_name: str,
    *,
    provider: LLMProvider,
    workspace: Path,
    max_cost: float,
    store: SQLAlchemyCheckpointStore,
) -> Workflow:
    if workflow_name == "pr-review":
        return build_pr_review_workflow(
            provider,
            workspace_root=workspace,
            max_cost_usd=max_cost,
            store=store,
        )
    if workflow_name == "issue-to-pr":
        return build_issue_to_pr_workflow(
            provider,
            workspace_root=workspace,
            max_cost_usd=max_cost,
            store=store,
        )
    raise typer.BadParameter(
        f"Unknown workflow '{workflow_name}'. Available: pr-review, issue-to-pr."
    )


def _render_run_result(result: RunResult) -> None:
    table = Table(title=f"Workflow Run: {result.workflow_name} ({result.run_id})")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    status_color = "green" if result.status == RunStatus.COMPLETED else "yellow"
    table.add_row("Status", f"[{status_color}]{result.status.value}[/{status_color}]")
    table.add_row("Steps Executed", str(len(result.steps)))
    table.add_row("Tool Calls", str(len(result.tool_calls)))
    table.add_row(
        "Tokens (in / out / total)",
        f"{result.total_input_tokens} / {result.total_output_tokens} / {result.total_tokens}",
    )
    table.add_row("Total Cost (USD)", f"${result.total_cost_usd:.4f}")
    if result.approval_id:
        table.add_row("Pending Approval ID", f"[bold yellow]{result.approval_id}[/bold yellow]")
    console.print(table)

    if result.tool_calls:
        tc_table = Table(title="Executed Tool Calls")
        tc_table.add_column("Call ID", style="dim")
        tc_table.add_column("Tool", style="magenta")
        tc_table.add_column("Risk Tier", style="cyan")
        tc_table.add_column("Status", style="green")
        tc_table.add_column("Latency", justify="right")
        for tc in result.tool_calls:
            tc_table.add_row(
                tc.call_id,
                tc.tool_name,
                tc.risk_tier.value,
                tc.status.value,
                f"{tc.latency_ms}ms",
            )
        console.print(tc_table)

    if result.output:
        console.print(Panel(result.output, title="Workflow Output", border_style="green"))

    if result.status == RunStatus.WAITING_APPROVAL and result.approval_id:
        console.print(
            Panel(
                f"Run paused for Human-in-the-Loop approval.\n"
                f"To approve: [bold cyan]runlatch approve {result.approval_id}[/bold cyan]\n"
                f"To reject:  [bold red]runlatch reject {result.approval_id}[/bold red]",
                title="Action Required",
                border_style="yellow",
            )
        )


@app.command("init")
def init_cmd(
    path: Annotated[
        Path, typer.Option("--path", help="Directory for runlatch.toml")
    ] = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing runlatch.toml")
    ] = False,
) -> None:
    """Initialize a default runlatch.toml policy configuration file."""
    target = path.resolve() / "runlatch.toml"
    if target.exists() and not force:
        console.print(f"[yellow]runlatch.toml already exists at {target}[/yellow]")
        raise typer.Exit(code=0)
    target.write_text(DEFAULT_TOML_TEMPLATE, encoding="utf-8")
    console.print(f"[green]Initialized {target}[/green]")


@app.command("run")
def run_cmd(
    workflow_name: Annotated[
        str, typer.Argument(help="Workflow to run (pr-review, issue-to-pr)")
    ],
    repo: Annotated[str, typer.Option("--repo", help="Target GitHub repository")] = "local/repo",
    pr: Annotated[int, typer.Option("--pr", help="Pull Request number")] = 42,
    issue: Annotated[int, typer.Option("--issue", help="Issue number")] = 108,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="LLM provider (ollama, groq, github-models, openai)"),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override model identifier")
    ] = None,
    max_cost: Annotated[
        float, typer.Option("--max-cost", help="Hard USD budget ceiling")
    ] = 0.15,
    demo: Annotated[
        bool, typer.Option("--demo", help="Run with deterministic $0 MockLLMProvider trace")
    ] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Repository workspace root")
    ] = Path("."),
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """Execute a built-in developer workflow locally with SQLite checkpointing."""
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url

    async def _run() -> RunResult:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            llm = _resolve_provider(
                workflow_name=workflow_name,
                demo=demo,
                provider_name=provider,
                model=model,
                issue=issue,
                settings=settings,
            )
            wf = _build_workflow(
                workflow_name,
                provider=llm,
                workspace=workspace.resolve(),
                max_cost=max_cost,
                store=store,
            )
            return await wf.run(
                {
                    "repository": repo,
                    "pr_number": pr,
                    "issue_number": issue,
                    "demo": demo,
                }
            )
        finally:
            await store.dispose()

    try:
        result = asyncio.run(_run())
    except RunLatchError as exc:
        console.print(f"[bold red]RunLatchError:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_run_result(result)


@runs_app.command("list")
def runs_list_cmd(
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by RunStatus")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum runs to display")] = 20,
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """List persisted workflow runs from CheckpointStore."""
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url

    async def _fetch() -> list[object]:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            return list(await store.list_checkpoints(status=status, limit=limit))
        finally:
            await store.dispose()

    checkpoints = asyncio.run(_fetch())
    table = Table(title="Persisted Workflow Runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Workflow", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Step", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    for cp in checkpoints:
        from runlatch.core.types import RunCheckpoint

        assert isinstance(cp, RunCheckpoint)
        tok = cp.total_input_tokens + cp.total_output_tokens
        table.add_row(
            str(cp.run_id),
            cp.workflow_name,
            cp.status.value,
            str(cp.current_step_index),
            str(tok),
            f"${cp.total_cost_usd:.4f}",
        )
    console.print(table)


@runs_app.command("inspect")
def runs_inspect_cmd(
    run_id: Annotated[str, typer.Argument(help="UUID of the workflow run")],
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """Inspect full details, steps, and tool calls for a specific run."""
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url
    parsed_id = UUID(run_id)

    async def _load() -> tuple[object, list[object], list[object]]:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            cp = await store.get_checkpoint(parsed_id)
            steps = await store.list_steps(parsed_id)
            tcs = await store.list_tool_calls(parsed_id)
            return cp, list(steps), list(tcs)
        finally:
            await store.dispose()

    cp, steps, tcs = asyncio.run(_load())
    if cp is None:
        console.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(code=1)

    from runlatch.core.types import RunCheckpoint, StepRecord, ToolCallRecord

    assert isinstance(cp, RunCheckpoint)
    console.print(
        Panel(
            f"Run ID: {cp.run_id}\n"
            f"Workflow: {cp.workflow_name}\n"
            f"Status: {cp.status.value}\n"
            f"Tokens: {cp.total_input_tokens + cp.total_output_tokens}\n"
            f"Cost: ${cp.total_cost_usd:.4f}",
            title="Run Summary",
        )
    )
    if steps:
        st_table = Table(title="Run Steps")
        st_table.add_column("#", justify="right")
        st_table.add_column("Step")
        st_table.add_column("Agent")
        st_table.add_column("Status")
        for s in steps:
            assert isinstance(s, StepRecord)
            st_table.add_row(str(s.step_index), s.step_name, s.agent_name, s.status.value)
        console.print(st_table)

    if tcs:
        tc_table = Table(title="Tool Calls")
        tc_table.add_column("Call ID")
        tc_table.add_column("Tool")
        tc_table.add_column("Tier")
        tc_table.add_column("Status")
        for t in tcs:
            assert isinstance(t, ToolCallRecord)
            tc_table.add_row(t.call_id, t.tool_name, t.risk_tier.value, t.status.value)
        console.print(tc_table)


@approvals_app.command("list")
def approvals_list_cmd(
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by ApprovalStatus")
    ] = "PENDING",
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """List pending or decided Human-in-the-Loop approval requests."""
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url
    filter_status = ApprovalStatus(status.upper()) if status else None

    async def _fetch() -> list[object]:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            return list(await store.list_approvals(status=filter_status))
        finally:
            await store.dispose()

    items = asyncio.run(_fetch())
    table = Table(title="HITL Approval Requests")
    table.add_column("Approval ID", style="yellow")
    table.add_column("Run ID", style="cyan")
    table.add_column("Tool", style="magenta")
    table.add_column("Tier")
    table.add_column("Status")
    for item in items:
        from runlatch.core.types import ApprovalRequestRecord

        assert isinstance(item, ApprovalRequestRecord)
        table.add_row(
            str(item.id),
            str(item.run_id),
            item.tool_name,
            item.risk_tier.value,
            item.status.value,
        )
    console.print(table)


def _decide_and_resume(
    approval_id: str,
    *,
    approved: bool,
    reason: str,
    actor: str,
    demo: bool,
    workspace: Path,
    db_url: str | None,
) -> None:
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url
    parsed_approval_id = UUID(approval_id)

    async def _op() -> RunResult:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            approval = await store.get_approval(parsed_approval_id)
            if approval is None:
                raise RunLatchError(f"Approval {approval_id} not found.")
            cp = await store.get_checkpoint(approval.run_id)
            if cp is None:
                raise RunLatchError(f"Checkpoint {approval.run_id} not found.")

            issue_num = int(cp.input_payload.get("issue_number", 108))
            use_demo = (
                demo
                or (cp.input_payload.get("demo") is True)
                or settings.llm_provider in ("mock", "demo")
            )
            if use_demo:
                llm: LLMProvider = MockLLMProvider(
                    [issue_to_pr_demo_responses(issue_num)[-1]], model="demo"
                )
            else:
                llm = _resolve_provider(
                    workflow_name=cp.workflow_name,
                    demo=False,
                    provider_name=None,
                    model=None,
                    issue=issue_num,
                    settings=settings,
                )

            wf = _build_workflow(
                cp.workflow_name,
                provider=llm,
                workspace=workspace.resolve(),
                max_cost=cp.max_cost_usd,
                store=store,
            )
            return await wf.resume(
                cp.run_id,
                approved=approved,
                decided_by=actor,
                reason=reason,
                store=store,
            )
        finally:
            await store.dispose()

    try:
        result = asyncio.run(_op())
    except RunLatchError as exc:
        console.print(f"[bold red]RunLatchError:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_run_result(result)


@app.command("approve")
def approve_cmd(
    approval_id: Annotated[str, typer.Argument(help="Approval UUID to approve")],
    reason: Annotated[
        str, typer.Option("--reason", help="Approval rationale")
    ] = "Approved via CLI",
    by: Annotated[str, typer.Option("--by", help="Reviewer identity")] = "cli-user",
    demo: Annotated[
        bool, typer.Option("--demo/--no-demo", help="Use demo completion provider")
    ] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Repository workspace root")
    ] = Path("."),
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """Approve a pending HITL request and resume the paused workflow."""
    _decide_and_resume(
        approval_id,
        approved=True,
        reason=reason,
        actor=by,
        demo=demo,
        workspace=workspace,
        db_url=db_url,
    )


@app.command("reject")
def reject_cmd(
    approval_id: Annotated[str, typer.Argument(help="Approval UUID to reject")],
    reason: Annotated[
        str, typer.Option("--reason", help="Rejection rationale")
    ] = "Rejected via CLI",
    by: Annotated[str, typer.Option("--by", help="Reviewer identity")] = "cli-user",
    demo: Annotated[
        bool, typer.Option("--demo/--no-demo", help="Use demo completion provider")
    ] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Repository workspace root")
    ] = Path("."),
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """Reject a pending HITL request and feed the rejection reason back to the workflow."""
    _decide_and_resume(
        approval_id,
        approved=False,
        reason=reason,
        actor=by,
        demo=demo,
        workspace=workspace,
        db_url=db_url,
    )


@app.command("replay")
def replay_cmd(
    run_id: Annotated[str, typer.Argument(help="Run UUID to replay")],
    demo: Annotated[
        bool, typer.Option("--demo/--no-demo", help="Use demo provider for replay")
    ] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", help="Repository workspace root")
    ] = Path("."),
    db_url: Annotated[
        str | None, typer.Option("--db", help="Override SQLAlchemy database URL")
    ] = None,
) -> None:
    """Clone input payload from a previous run and execute a deterministic replay."""
    settings = RunLatchSettings.load()
    active_db = db_url or settings.database_url
    parsed_id = UUID(run_id)

    async def _replay() -> RunResult:
        store = SQLAlchemyCheckpointStore(active_db)
        try:
            cp = await store.get_checkpoint(parsed_id)
            if cp is None:
                raise RunLatchError(f"Run {run_id} not found.")
            issue_num = int(cp.input_payload.get("issue_number", 108))
            use_demo = demo or (cp.input_payload.get("demo") is True)
            llm = _resolve_provider(
                workflow_name=cp.workflow_name,
                demo=use_demo,
                provider_name=None,
                model=None,
                issue=issue_num,
                settings=settings,
            )
            wf = _build_workflow(
                cp.workflow_name,
                provider=llm,
                workspace=workspace.resolve(),
                max_cost=cp.max_cost_usd,
                store=store,
            )
            return await wf.run(cp.input_payload)
        finally:
            await store.dispose()

    try:
        result = asyncio.run(_replay())
    except RunLatchError as exc:
        console.print(f"[bold red]RunLatchError:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_run_result(result)
