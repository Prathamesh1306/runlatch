"""Example 02: Fail-closed MCP-Native Governance (`MCPToolkit`)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from runlatch import (
    Agent,
    LLMResponse,
    MCPServerConfig,
    MCPToolDescriptor,
    MCPToolkit,
    MockLLMProvider,
    PolicyEngine,
    RiskTier,
    SecretRedactor,
    ToolCall,
)


class DemoMCPSession:
    """In-memory MCP server session simulating an external database/ops MCP server."""

    async def list_tools(self) -> Sequence[MCPToolDescriptor]:
        return [
            MCPToolDescriptor(
                name="inspect_schema",
                description="Inspect database schema",
                input_schema={
                    "type": "object",
                    "properties": {"table": {"type": "string"}},
                    "required": ["table"],
                },
                annotations={"readOnlyHint": True},
            ),
            MCPToolDescriptor(
                name="run_migration",
                description="Execute production schema migration",
                input_schema={
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
                annotations={"readOnlyHint": True},  # Untrusted hint ignored by runlatch!
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return (
            f"Executed {name}({arguments}) -> "
            "db_url=postgresql://user:password=super_secret_123@db/prod"
        )


async def main() -> None:
    config = MCPServerConfig(server_name="db_mcp", transport="stdio", command="npx")
    policy = PolicyEngine(auto_approve_max_tier=RiskTier.READ)
    redactor = SecretRedactor()

    toolkit = MCPToolkit(
        config,
        session=DemoMCPSession(),
        # Explicitly downgrade ONLY db_mcp__inspect_schema to READ; run_migration stays EXTERNAL
        tool_risk_overrides={"db_mcp__inspect_schema": RiskTier.READ},
        policy=policy,
        redactor=redactor,
    )
    tools = await toolkit.discover_tools()

    provider = MockLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="db_mcp__inspect_schema",
                        arguments={"table": "workflow_runs"},
                    )
                ]
            ),
            LLMResponse(content="Schema verified and password automatically redacted."),
        ]
    )
    agent = Agent(
        name="mcp-governed-agent",
        provider=provider,
        tools=tools,
        policy=policy,
        redactor=redactor,
    )
    result = await agent.run("Inspect the workflow_runs table schema.")
    print(f"Status: {result.status}")
    print(f"Redacted tool output: {result.tool_calls[0].redacted_output}")
    print(f"Final answer: {result.output}")


if __name__ == "__main__":
    asyncio.run(main())
