"""Integration test for Stateless HITL Pause -> SQLite Checkpoint -> Worktree Resume.

Verifies:
1. Workflow pauses on a RiskTier.EXTERNAL tool call.
2. GitWorkspacePatcher captures BOTH modified tracked files AND brand-new untracked files
   using `git add -N .` + `git diff HEAD`.
3. Checkpoint + workspace_patch + workspace_meta are persisted to SQLite via
   SQLAlchemyCheckpointStore.
4. Destroying the in-memory Agent/Workflow and creating a brand-new instance resumes cleanly:
   - When approved=True: reconstructs repo + untracked file in a fresh git worktree via
     `git apply --3way`, executes the tool, and finishes the workflow.
   - When approved=False: feeds rejection reason back to the LLM without executing the tool.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.core.agent import Agent
from runlatch.core.policy import PolicyEngine
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import ApprovalStatus, LLMResponse, RiskTier, RunStatus, ToolCall
from runlatch.core.workflow import Step, Workflow
from runlatch.tools.git_patcher import GitWorkspacePatcher
from runlatch.tools.registry import tool


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@runlatch.dev"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "RunLatch Test"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    (repo_dir / "app.py").write_text("def greet() -> str:\n    return 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_hitl_pause_with_untracked_files_and_worktree_resume_approved(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    worktree_dir = tmp_path / "resumed_worktree"
    db_path = tmp_path / "test_hitl.db"
    _init_git_repo(repo_dir)

    # Simulate edits made during the run: 1 modified tracked file + 1 brand-new untracked file
    (repo_dir / "app.py").write_text("def greet() -> str:\n    return 'new'\n", encoding="utf-8")
    (repo_dir / "test_app.py").write_text(
        "from app import greet\n\ndef test_greet() -> None:\n    assert greet() == 'new'\n",
        encoding="utf-8",
    )

    executed_prs: list[dict[str, str]] = []

    @tool(risk=RiskTier.EXTERNAL, requires_approval=True)
    async def github_create_pr(title: str, head: str, base: str = "main") -> str:
        """Open a pull request."""
        executed_prs.append({"title": title, "head": head, "base": base})
        return f"https://github.com/org/repo/pull/99 ({title})"

    store = SQLAlchemyCheckpointStore(f"sqlite+aiosqlite:///{db_path}")
    patcher = GitWorkspacePatcher(repo_dir, worktree_target_dir=worktree_dir)
    policy = PolicyEngine(auto_approve_max_tier=RiskTier.WRITE)
    redactor = SecretRedactor()

    provider_phase1 = MockLLMProvider(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_pr_1",
                        name="github_create_pr",
                        arguments={"title": "fix: update greet", "head": "fix/greet"},
                    )
                ],
                input_tokens=120,
                output_tokens=30,
                cost_usd=0.005,
            )
        ]
    )

    agent_instance_1 = Agent(
        name="publisher",
        provider=provider_phase1,
        tools=[github_create_pr],
        policy=policy,
        redactor=redactor,
        patcher=patcher,
        store=store,
    )
    workflow_instance_1 = Workflow(name="issue-to-pr", store=store).add_step(
        Step(name="publish", agent=agent_instance_1)
    )

    paused_result = await workflow_instance_1.run({"issue_number": 108})

    assert paused_result.status == RunStatus.WAITING_APPROVAL
    assert paused_result.approval_id is not None
    assert len(executed_prs) == 0
    assert paused_result.workspace_patch is not None
    # Verify brand-new untracked file 'test_app.py' was captured thanks to `git add -N .`
    assert "test_app.py" in paused_result.workspace_patch
    assert "app.py" in paused_result.workspace_patch

    run_id = paused_result.run_id
    approval_id = paused_result.approval_id

    # Destroy in-memory agent & workflow; create a fresh store & agent (simulating Worker 2)
    await store.dispose()
    del agent_instance_1
    del workflow_instance_1

    store_worker2 = SQLAlchemyCheckpointStore(
        f"sqlite+aiosqlite:///{db_path}", use_null_pool=True
    )
    patcher_worker2 = GitWorkspacePatcher(repo_dir, worktree_target_dir=worktree_dir)
    provider_phase2 = MockLLMProvider(
        responses=[
            LLMResponse(
                content="PR opened successfully at https://github.com/org/repo/pull/99",
                tool_calls=[],
                input_tokens=150,
                output_tokens=25,
                cost_usd=0.004,
            )
        ]
    )
    agent_instance_2 = Agent(
        name="publisher",
        provider=provider_phase2,
        tools=[github_create_pr],
        policy=policy,
        redactor=redactor,
        patcher=patcher_worker2,
        store=store_worker2,
    )
    workflow_instance_2 = Workflow(name="issue-to-pr", store=store_worker2).add_step(
        Step(name="publish", agent=agent_instance_2)
    )

    resumed_result = await workflow_instance_2.resume(
        run_id,
        approved=True,
        decided_by="prathamesh",
        reason="LGTM, tests pass",
    )

    assert resumed_result.status == RunStatus.COMPLETED
    assert "https://github.com/org/repo/pull/99" in resumed_result.output
    assert len(executed_prs) == 1
    assert executed_prs[0]["title"] == "fix: update greet"

    # Verify fresh git worktree has BOTH the modified file and the untracked file applied!
    assert (worktree_dir / "app.py").read_text(encoding="utf-8") == (
        "def greet() -> str:\n    return 'new'\n"
    )
    assert (worktree_dir / "test_app.py").is_file()
    assert "assert greet() == 'new'" in (worktree_dir / "test_app.py").read_text(encoding="utf-8")

    # Verify approval record is marked APPROVED in SQLite
    approval_row = await store_worker2.get_approval(approval_id)
    assert approval_row is not None
    assert approval_row.status == ApprovalStatus.APPROVED
    assert approval_row.decided_by == "prathamesh"
    await store_worker2.dispose()


@pytest.mark.asyncio
async def test_hitl_resume_rejected_feeds_reason_to_llm_without_executing_tool(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test_reject.db"
    store = SQLAlchemyCheckpointStore(f"sqlite+aiosqlite:///{db_path}")

    called = False

    @tool(risk=RiskTier.EXTERNAL, requires_approval=True)
    async def dangerous_push(branch: str) -> str:
        """Push branch to remote."""
        nonlocal called
        called = True
        return f"pushed {branch}"

    provider_1 = MockLLMProvider(
        responses=[
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c_push", name="dangerous_push", arguments={"branch": "main"})
                ]
            )
        ]
    )
    agent_1 = Agent(name="pusher", provider=provider_1, tools=[dangerous_push], store=store)
    paused = await agent_1.run("Push directly to main")
    assert paused.status == RunStatus.WAITING_APPROVAL
    assert not called

    provider_2 = MockLLMProvider(
        responses=[
            LLMResponse(
                content="Understood, I will not push directly to main.",
                tool_calls=[],
            )
        ]
    )
    agent_2 = Agent(name="pusher", provider=provider_2, tools=[dangerous_push], store=store)
    resumed = await agent_2.resume(
        paused.run_id,
        approved=False,
        decided_by="lead-dev",
        reason="Never push directly to main",
    )

    assert resumed.status == RunStatus.COMPLETED
    assert not called
    assert "will not push" in resumed.output
    # Ensure the rejection reason was passed into the LLM transcript
    last_tool_msg = [m for m in provider_2.calls[0]["messages"] if m.role == "tool"][-1]
    assert "Never push directly to main" in (last_tool_msg.content or "")
    await store.dispose()


@pytest.mark.asyncio
async def test_hitl_resume_preserves_steps_taken_and_sibling_tool_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test_siblings_and_steps.db"
    store = SQLAlchemyCheckpointStore(f"sqlite+aiosqlite:///{db_path}")

    executed_order: list[str] = []

    @tool(risk=RiskTier.READ)
    async def read_step(label: str) -> str:
        """Read-only sibling tool."""
        executed_order.append(f"read:{label}")
        return f"read_ok:{label}"

    @tool(risk=RiskTier.EXTERNAL, requires_approval=True)
    async def external_step(label: str) -> str:
        """External tool requiring HITL approval."""
        executed_order.append(f"ext:{label}")
        return f"ext_ok:{label}"

    # Assistant returns 3 tool calls in ONE turn: [read_1, external_2 (pauses!), read_3 (sibling!)]
    provider_1 = MockLLMProvider(
        responses=[
            LLMResponse(
                tool_calls=[
                    ToolCall(id="tc_1", name="read_step", arguments={"label": "before"}),
                    ToolCall(id="tc_2", name="external_step", arguments={"label": "middle"}),
                    ToolCall(id="tc_3", name="read_step", arguments={"label": "after"}),
                ]
            )
        ]
    )
    agent_1 = Agent(
        name="sibling-agent",
        provider=provider_1,
        tools=[read_step, external_step],
        max_steps=2,
        store=store,
    )
    paused = await agent_1.run("Run three sibling tools")
    assert paused.status == RunStatus.WAITING_APPROVAL
    assert executed_order == ["read:before"]

    cp = await store.get_checkpoint(paused.run_id)
    assert cp is not None
    assert cp.steps_taken == 1
    assert [t.id for t in cp.remaining_tool_calls] == ["tc_3"]

    # On resume, tc_2 AND sibling tc_3 must BOTH execute before the 2nd LLM call
    provider_2 = MockLLMProvider(
        responses=[
            LLMResponse(content="All three sibling tool calls completed.", tool_calls=[])
        ]
    )
    agent_2 = Agent(
        name="sibling-agent",
        provider=provider_2,
        tools=[read_step, external_step],
        max_steps=2,
        store=store,
    )
    resumed = await agent_2.resume(paused.run_id, approved=True, decided_by="tester")
    assert resumed.status == RunStatus.COMPLETED
    assert executed_order == ["read:before", "ext:middle", "read:after"]
    assert resumed.steps_taken == 2

    # Verify all 3 tool_call_ids (tc_1, tc_2, tc_3) were present in the transcript sent to LLM
    sent_messages = provider_2.calls[0]["messages"]
    tool_call_ids = [m.tool_call_id for m in sent_messages if m.role == "tool"]
    assert tool_call_ids == ["tc_1", "tc_2", "tc_3"]

    # Also verify max_steps cannot be bypassed across pause/resume (max_steps=1 fails on resume)
    from runlatch.core.exceptions import BudgetExceededError

    provider_3 = MockLLMProvider(
        responses=[
            LLMResponse(
                tool_calls=[
                    ToolCall(id="tc_only", name="external_step", arguments={"label": "cap"})
                ]
            )
        ]
    )
    agent_3 = Agent(
        name="step-capped",
        provider=provider_3,
        tools=[external_step],
        max_steps=1,
        store=store,
    )
    paused_cap = await agent_3.run("Pause at step 1 of 1")
    assert paused_cap.status == RunStatus.WAITING_APPROVAL

    with pytest.raises(BudgetExceededError) as exc_info:
        await agent_3.resume(paused_cap.run_id, approved=True)
    assert exc_info.value.metric == "max_steps"
    await store.dispose()
