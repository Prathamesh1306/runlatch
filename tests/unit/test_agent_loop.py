"""Unit tests for Agent ReAct loop, parallel READ calls, loop detection, and budgets."""

from __future__ import annotations

import asyncio

import pytest

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.core.agent import Agent
from runlatch.core.exceptions import BudgetExceededError, LoopDetectedError
from runlatch.core.types import LLMResponse, RiskTier, RunStatus, ToolCall
from runlatch.tools.registry import tool


@tool(risk=RiskTier.READ)
async def inspect_file(path: str) -> str:
    """Read file summary."""
    await asyncio.sleep(0.01)
    return f"contents_of:{path}"


@pytest.mark.asyncio
async def test_agent_parallel_read_tools_and_completion() -> None:
    provider = MockLLMProvider(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="inspect_file", arguments={"path": "a.py"}),
                    ToolCall(id="c2", name="inspect_file", arguments={"path": "b.py"}),
                ],
                input_tokens=50,
                output_tokens=20,
                cost_usd=0.001,
            ),
            LLMResponse(
                content="Both files inspected cleanly.",
                tool_calls=[],
                input_tokens=80,
                output_tokens=15,
                cost_usd=0.002,
            ),
        ]
    )

    agent = Agent(name="inspector", provider=provider, tools=[inspect_file])
    result = await agent.run("Check a.py and b.py")

    assert result.status == RunStatus.COMPLETED
    assert result.output == "Both files inspected cleanly."
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].redacted_output == "contents_of:a.py"
    assert result.tool_calls[1].redacted_output == "contents_of:b.py"
    assert result.total_tokens == 165
    assert result.total_cost_usd == pytest.approx(0.003)
    assert result.steps_taken == 2


@pytest.mark.asyncio
async def test_agent_loop_detection_aborts_on_third_duplicate_call() -> None:
    dup_call = ToolCall(id="c_dup", name="inspect_file", arguments={"path": "same.py"})
    provider = MockLLMProvider(
        responses=[
            LLMResponse(tool_calls=[dup_call]),
            LLMResponse(tool_calls=[dup_call]),
            LLMResponse(tool_calls=[dup_call]),
        ]
    )
    agent = Agent(
        name="looper",
        provider=provider,
        tools=[inspect_file],
        max_duplicate_tool_calls=2,
    )

    with pytest.raises(LoopDetectedError) as exc_info:
        await agent.run("Keep reading same.py")
    assert exc_info.value.tool_name == "inspect_file"
    assert exc_info.value.count == 3


@pytest.mark.asyncio
async def test_agent_budget_caps_enforced() -> None:
    provider = MockLLMProvider(
        responses=[
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="inspect_file", arguments={"path": "1.py"})],
                input_tokens=500,
                output_tokens=500,
                cost_usd=0.10,
            ),
        ]
    )
    agent = Agent(
        name="budgeted",
        provider=provider,
        tools=[inspect_file],
        max_cost_usd=0.05,
    )
    with pytest.raises(BudgetExceededError) as exc_info:
        await agent.run("Exceed cost")
    assert exc_info.value.metric == "max_cost_usd"
