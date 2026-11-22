"""Unit tests for @tool schema compilation, argument validation, and truncation."""

from __future__ import annotations

import asyncio

import pytest

from runlatch.core.types import RiskTier
from runlatch.tools.registry import ToolRegistry, tool, truncate_output


@tool(risk=RiskTier.READ)
def add_numbers(a: int, b: int = 5) -> int:
    """Add two integers together."""
    return a + b


@tool(risk=RiskTier.EXTERNAL, timeout_s=0.1, max_output_chars=100)
async def deploy_service(env: str, dry_run: bool = True) -> dict[str, object]:
    """Deploy service to target environment."""
    return {"env": env, "dry_run": dry_run, "status": "deployed"}


@tool(timeout_s=0.05)
async def slow_tool(delay: float) -> str:
    """Sleep longer than timeout."""
    await asyncio.sleep(delay)
    return "done"


@pytest.mark.asyncio
async def test_tool_schema_compilation() -> None:
    spec = add_numbers.spec
    assert spec.name == "add_numbers"
    assert spec.description == "Add two integers together."
    assert spec.risk_tier == RiskTier.READ
    assert spec.requires_approval is False
    assert "a" in spec.parameters["properties"]
    assert "b" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["a"]


@pytest.mark.asyncio
async def test_external_tool_defaults_to_requires_approval() -> None:
    assert deploy_service.spec.risk_tier == RiskTier.EXTERNAL
    assert deploy_service.spec.requires_approval is True


@pytest.mark.asyncio
async def test_tool_invocation_and_self_correction_validation_error() -> None:
    out = await add_numbers.invoke({"a": 10, "b": 7})
    assert out == "17"

    # Malformed arguments should NOT raise an unhandled exception; returns error string for LLM
    bad_out = await add_numbers.invoke({"a": "not-an-int"})
    assert bad_out.startswith("ToolValidationError:")
    assert "add_numbers" in bad_out


@pytest.mark.asyncio
async def test_tool_timeout_and_truncation() -> None:
    with pytest.raises(TimeoutError):
        await slow_tool.invoke({"delay": 0.3})

    huge = "A" * 500 + "B" * 500
    truncated = truncate_output(huge, max_chars=120)
    assert len(truncated) <= 120
    assert "[truncated 880 chars]" in truncated
    assert truncated.startswith("A")
    assert truncated.endswith("B")


def test_tool_registry() -> None:
    reg = ToolRegistry([add_numbers, deploy_service])
    assert len(reg) == 2
    assert "add_numbers" in reg
    assert reg.get("deploy_service") is deploy_service
    assert len(reg.specs()) == 2
