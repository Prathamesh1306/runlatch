"""Unit tests for fail-closed MCPToolkit governance, globs, session reuse, and live args."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.adapters.storage.sqlalchemy_store import SQLAlchemyCheckpointStore
from runlatch.core.agent import Agent
from runlatch.core.exceptions import PolicyDeniedError
from runlatch.core.policy import PolicyEngine
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import LLMResponse, RiskTier, RunStatus, ToolCall
from runlatch.toolkits.mcp import MCPServerConfig, MCPToolDescriptor, MCPToolkit


class FakeMCPSession:
    """In-memory MCPClientSession tracking connection lifecycle and live arguments."""

    def __init__(self) -> None:
        self.called: list[tuple[str, dict[str, Any]]] = []
        self.connect_count = 0
        self.close_count = 0
        self.is_open = False

    async def connect(self) -> None:
        self.connect_count += 1
        self.is_open = True

    async def close(self) -> None:
        self.close_count += 1
        self.is_open = False

    async def list_tools(self) -> Sequence[MCPToolDescriptor]:
        return [
            MCPToolDescriptor(
                name="query_logs",
                description="Query production logs",
                input_schema={
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "api_key": {"type": "string"},
                    },
                    "required": ["service"],
                },
                annotations={"readOnlyHint": True},
            ),
            MCPToolDescriptor(
                name="get_metrics",
                description="Read service metrics",
                input_schema={"type": "object", "properties": {}},
                annotations={"readOnlyHint": True},
            ),
            MCPToolDescriptor(
                name="get_secret_dump",
                description="Dump secrets",
                input_schema={"type": "object", "properties": {}},
                annotations={"readOnlyHint": True},
            ),
            MCPToolDescriptor(
                name="delete_cluster",
                description="Delete prod cluster",
                input_schema={"type": "object", "properties": {}},
                annotations={"readOnlyHint": True},
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.called.append((name, dict(arguments)))
        svc = arguments.get("service", "default")
        return f"Log line for {svc} with leaked key sk-proj-1234567890abcdefghijklmno"


@pytest.mark.asyncio
async def test_mcp_toolkit_fail_closed_ignores_remote_readonly_hint() -> None:
    session = FakeMCPSession()
    config = MCPServerConfig(server_name="ops", transport="stdio", command="npx")
    toolkit = MCPToolkit(config, session=session)
    tools = await toolkit.discover_tools()

    assert len(tools) == 4
    by_name = {t.spec.name: t for t in tools}

    # Namespaced as {server}__{tool}
    assert "ops__query_logs" in by_name
    assert "ops__delete_cluster" in by_name

    # All MUST default to RiskTier.EXTERNAL despite remote readOnlyHint=True
    for tool_obj in by_name.values():
        assert tool_obj.spec.risk_tier == RiskTier.EXTERNAL
        assert tool_obj.spec.requires_approval is True


@pytest.mark.asyncio
async def test_mcp_toolkit_glob_overrides_exact_precedence_and_async_context_manager() -> None:
    session = FakeMCPSession()
    config = MCPServerConfig(server_name="ops", transport="stdio", command="npx")

    async with MCPToolkit(
        config,
        session=session,
        tool_risk_overrides={
            # Glob pattern downgrades ops__get_* to READ
            "ops__get_*": RiskTier.READ,
            # Exact match wins over glob and keeps get_secret_dump at EXTERNAL
            "ops__get_secret_dump": RiskTier.EXTERNAL,
            "query_logs": RiskTier.READ,
        },
    ) as toolkit:
        assert session.connect_count == 1
        assert session.is_open is True

        tools = await toolkit.discover_tools()
        by_name = {t.spec.name: t for t in tools}

        assert by_name["ops__get_metrics"].spec.risk_tier == RiskTier.READ
        assert by_name["ops__get_secret_dump"].spec.risk_tier == RiskTier.EXTERNAL
        assert by_name["ops__query_logs"].spec.risk_tier == RiskTier.READ
        assert by_name["ops__delete_cluster"].spec.risk_tier == RiskTier.EXTERNAL

        # Multiple tool calls inside `async with` reuse the same session (connect_count stays 1)
        await by_name["ops__get_metrics"].invoke({})
        await by_name["ops__query_logs"].invoke({"service": "billing"})
        assert session.connect_count == 1

    assert session.close_count == 1
    assert session.is_open is False


@pytest.mark.asyncio
async def test_mcp_toolkit_passes_unredacted_live_args_but_redacts_output_and_audit_logs(
    tmp_path: Any,
) -> None:
    session = FakeMCPSession()
    config = MCPServerConfig(
        server_name="ops",
        transport="streamable_http",
        url="http://localhost:9000/mcp",
    )
    policy = PolicyEngine(
        auto_approve_max_tier=RiskTier.READ,
        denied_tools=["ops__delete_cluster"],
    )
    redactor = SecretRedactor()

    toolkit = MCPToolkit(
        config,
        session=session,
        tool_risk_overrides={"ops__query_logs": RiskTier.READ},
        policy=policy,
        redactor=redactor,
        enforce_policy_on_direct_invoke=True,
    )
    tools = await toolkit.discover_tools()
    by_name = {t.spec.name: t for t in tools}

    live_secret = "sk-proj-liveSecretTokenForRemoteServer999"
    out = await by_name["ops__query_logs"].invoke(
        {"service": "auth-api", "api_key": live_secret}
    )

    # 1. Remote MCP server received the LIVE unredacted argument so the call works
    assert session.called[-1][1]["api_key"] == live_secret

    # 2. Output returned to LLM has secrets scrubbed
    assert "[REDACTED_OPENAI_KEY]" in out
    assert "sk-proj-" not in out

    # 3. Direct invocation of denied tool raises PolicyDeniedError
    with pytest.raises(PolicyDeniedError):
        await by_name["ops__delete_cluster"].invoke({})

    # 4. Inside Agent, audit record in SQLite redacts args while remote server gets live args
    store = SQLAlchemyCheckpointStore(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
    provider = MockLLMProvider(
        responses=[
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c_mcp_1",
                        name="ops__query_logs",
                        arguments={"service": "payments", "api_key": live_secret},
                    )
                ]
            ),
            LLMResponse(content="Done"),
        ]
    )
    agent = Agent(
        name="mcp-agent",
        provider=provider,
        tools=[by_name["ops__query_logs"]],
        policy=policy,
        redactor=redactor,
        store=store,
    )
    res = await agent.run("Query payments logs")
    assert res.status == RunStatus.COMPLETED
    assert session.called[-1][1]["api_key"] == live_secret
    assert res.tool_calls[0].redacted_args["api_key"] == "[REDACTED]"
    await store.dispose()
