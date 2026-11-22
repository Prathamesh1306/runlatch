"""Declarative PolicyEngine enforcing tool risk tiers, explicit overrides, and TOML rules.

Pure domain module: imports ONLY Python stdlib, pydantic, and runlatch.core.*.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from runlatch.core.types import PolicyAction, PolicyDecision, RiskTier, ToolSpec


class PolicyEngine:
    """Deterministic policy evaluator for native @tool and MCP tools."""

    def __init__(
        self,
        *,
        auto_approve_max_tier: RiskTier = RiskTier.WRITE,
        require_approval_tools: Sequence[str] = ("git_push", "github_create_pr"),
        denied_tools: Sequence[str] = (),
    ) -> None:
        self.auto_approve_max_tier = auto_approve_max_tier
        self.require_approval_tools: set[str] = set(require_approval_tools)
        self.denied_tools: set[str] = set(denied_tools)

    @classmethod
    def from_toml(cls, path: Path | str) -> PolicyEngine:
        """Construct a PolicyEngine from a runlatch.toml configuration file."""
        toml_path = Path(path)
        data: dict[str, Any] = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        policy_block = data.get("policy", {})
        if not isinstance(policy_block, dict):
            return cls()

        raw_tier = str(policy_block.get("auto_approve_max_tier", "WRITE")).upper()
        max_tier = RiskTier(raw_tier)
        req_tools = list(
            policy_block.get("require_approval_tools", ["git_push", "github_create_pr"])
        )
        den_tools = list(policy_block.get("denied_tools", []))
        return cls(
            auto_approve_max_tier=max_tier,
            require_approval_tools=req_tools,
            denied_tools=den_tools,
        )

    def evaluate(self, spec: ToolSpec, args: dict[str, Any]) -> PolicyDecision:
        """Evaluate whether a tool invocation is allowed, denied, or requires approval."""
        del args  # reserved for parameter-level predicates
        if spec.name in self.denied_tools:
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason=f"Tool '{spec.name}' is in policy.denied_tools.",
            )

        if spec.name in self.require_approval_tools or spec.requires_approval:
            return PolicyDecision(
                action=PolicyAction.REQUIRE_APPROVAL,
                reason=f"Tool '{spec.name}' requires explicit human approval.",
            )

        if spec.risk_tier > self.auto_approve_max_tier:
            return PolicyDecision(
                action=PolicyAction.REQUIRE_APPROVAL,
                reason=(
                    f"Tool '{spec.name}' risk tier ({spec.risk_tier}) exceeds "
                    f"auto_approve_max_tier ({self.auto_approve_max_tier})."
                ),
            )

        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason=f"Allowed within auto_approve_max_tier ({self.auto_approve_max_tier}).",
        )
