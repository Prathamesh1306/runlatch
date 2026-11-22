"""Unit tests for multi-step Workflow, conditional steps, BYO StepExecutor, and budget pooling."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.core.agent import Agent
from runlatch.core.exceptions import BudgetExceededError
from runlatch.core.protocols import CheckpointStore
from runlatch.core.types import BudgetConfig, LLMResponse, RunResult, RunStatus, StepStatus
from runlatch.core.workflow import Step, Workflow


class CustomDeterministicExecutor:
    """Example BYO-Agent StepExecutor that runs deterministic Python logic."""

    @property
    def name(self) -> str:
        return "custom-validator"

    async def execute_step(
        self,
        prompt: str,
        *,
        context: dict[str, Any],
        run_id: UUID | None = None,
        workflow_name: str = "workflow",
        step_index: int = 0,
        budget: BudgetConfig | None = None,
        store: CheckpointStore | None = None,
    ) -> RunResult:
        del prompt, step_index, budget, store
        context["tests_passed"] = True
        return RunResult(
            run_id=run_id or uuid4(),
            workflow_name=workflow_name,
            status=RunStatus.COMPLETED,
            output="VALIDATED_OK",
            context_state=context,
            total_input_tokens=10,
            total_output_tokens=10,
            total_tokens=20,
            total_cost_usd=0.01,
            steps_taken=1,
        )


@pytest.mark.asyncio
async def test_workflow_multi_step_with_byo_executor_and_conditions() -> None:
    planner = Agent(
        name="planner",
        provider=MockLLMProvider(
            [
                LLMResponse(
                    content="Plan: edit auth.py",
                    input_tokens=30,
                    output_tokens=20,
                    cost_usd=0.02,
                )
            ]
        ),
    )
    publisher = Agent(
        name="publisher",
        provider=MockLLMProvider(
            [
                LLMResponse(
                    content="PR #42 opened",
                    input_tokens=25,
                    output_tokens=15,
                    cost_usd=0.01,
                )
            ]
        ),
    )
    skipped_agent = Agent(
        name="never-runs",
        provider=MockLLMProvider([LLMResponse(content="Should not run")]),
    )

    wf = (
        Workflow(name="feature-pipeline", max_cost_usd=0.50)
        .add_step(Step(name="plan", agent=planner))
        .add_step(Step(name="validate", agent=CustomDeterministicExecutor()))
        .add_step(
            Step(
                name="publish",
                agent=publisher,
                condition=lambda ctx: ctx.get("tests_passed") is True,
            )
        )
        .add_step(
            Step(
                name="fallback",
                agent=skipped_agent,
                condition=lambda ctx: ctx.get("tests_passed") is False,
            )
        )
    )

    res = await wf.run({"issue": 108})
    assert res.status == RunStatus.COMPLETED
    assert res.output == "PR #42 opened"
    assert len(res.steps) == 4
    assert res.steps[0].status == StepStatus.COMPLETED
    assert res.steps[1].status == StepStatus.COMPLETED
    assert res.steps[2].status == StepStatus.COMPLETED
    assert res.steps[3].status == StepStatus.SKIPPED
    assert res.total_cost_usd == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_workflow_cross_step_budget_pooling() -> None:
    step1_agent = Agent(
        name="s1",
        provider=MockLLMProvider([LLMResponse(content="step1", cost_usd=0.08)]),
    )
    step2_agent = Agent(
        name="s2",
        provider=MockLLMProvider([LLMResponse(content="step2", cost_usd=0.05)]),
    )

    wf = (
        Workflow(name="tight-budget", max_cost_usd=0.10)
        .add_step(Step(name="first", agent=step1_agent))
        .add_step(Step(name="second", agent=step2_agent))
    )

    with pytest.raises(BudgetExceededError):
        await wf.run("Run with $0.10 pooled cap")
