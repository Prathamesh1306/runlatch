"""Unit tests for PolicyEngine risk tiers, explicit tool overrides, and TOML loading."""

from __future__ import annotations

from pathlib import Path

from runlatch.core.policy import PolicyEngine
from runlatch.core.types import PolicyAction, RiskTier, ToolSpec


def _spec(name: str, risk: RiskTier, requires_approval: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        risk_tier=risk,
        requires_approval=requires_approval,
    )


def test_policy_engine_tier_boundaries_and_overrides() -> None:
    engine = PolicyEngine(
        auto_approve_max_tier=RiskTier.WRITE,
        require_approval_tools=["git_commit"],
        denied_tools=["drop_database"],
    )

    assert engine.evaluate(_spec("read_file", RiskTier.READ), {}).action == PolicyAction.ALLOW
    assert engine.evaluate(_spec("edit_file", RiskTier.WRITE), {}).action == PolicyAction.ALLOW
    assert (
        engine.evaluate(_spec("run_pytest", RiskTier.EXEC), {}).action
        == PolicyAction.REQUIRE_APPROVAL
    )
    assert (
        engine.evaluate(_spec("git_push", RiskTier.EXTERNAL), {}).action
        == PolicyAction.REQUIRE_APPROVAL
    )
    assert (
        engine.evaluate(_spec("git_commit", RiskTier.WRITE), {}).action
        == PolicyAction.REQUIRE_APPROVAL
    )
    assert (
        engine.evaluate(_spec("drop_database", RiskTier.READ), {}).action == PolicyAction.DENY
    )


def test_policy_engine_loads_from_toml(tmp_path: Path) -> None:
    toml_file = tmp_path / "runlatch.toml"
    toml_file.write_text(
        """
[policy]
auto_approve_max_tier = "READ"
require_approval_tools = ["custom_publish"]
denied_tools = ["rm_all"]
""",
        encoding="utf-8",
    )
    engine = PolicyEngine.from_toml(toml_file)
    assert engine.auto_approve_max_tier == RiskTier.READ
    assert (
        engine.evaluate(_spec("edit_file", RiskTier.WRITE), {}).action
        == PolicyAction.REQUIRE_APPROVAL
    )
    assert engine.evaluate(_spec("rm_all", RiskTier.READ), {}).action == PolicyAction.DENY
