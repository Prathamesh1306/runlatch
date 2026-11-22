"""Fail-closed MCP-Native Governance Toolkit (`MCPToolkit`).

Invariants enforced (AGENTS.md §3.5):
1. Lazy-imports optional `mcp` library so core works without `mcp` installed.
2. Serializable `MCPServerConfig` supporting `stdio` and `streamable_http`.
3. Namespaces all discovered tools as `{server_name}__{tool_name}`.
4. Defaults ALL discovered tools to `RiskTier.EXTERNAL` (`requires_approval=True`).
5. NEVER blindly trusts remote `readOnlyHint` annotations to downgrade risk tier —
   only explicit local `tool_risk_overrides` (supporting exact match and `fnmatch` globs,
   where exact match wins over glob) can lower a tool's RiskTier.
6. Supports `async with MCPToolkit(...)` via `AsyncExitStack` so a single session is reused
   across `discover_tools()` and `call_tool()` calls without respawning per call.
7. Passes unmodified `args` to `session.call_tool()` while redacting outputs and audit logs.

Imports ONLY Python stdlib, pydantic, runlatch.core.*, and runlatch.tools.*.
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field

from runlatch.core.exceptions import (
    ApprovalRequiredSignal,
    PolicyDeniedError,
    RunLatchError,
)
from runlatch.core.policy import PolicyEngine
from runlatch.core.protocols import (
    PolicyEvaluator,
    RegisteredTool,
    SecretRedactorProtocol,
)
from runlatch.core.redactor import SecretRedactor
from runlatch.core.types import PolicyAction, RiskTier, ToolSpec
from runlatch.tools.registry import truncate_output


class MCPServerConfig(BaseModel):
    """Serializable configuration for an MCP server connection."""

    server_name: str
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 30.0


class MCPToolDescriptor(BaseModel):
    """Normalized descriptor of a remote tool advertised by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    annotations: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class MCPClientSession(Protocol):
    """Protocol for an active or mocked MCP client session."""

    async def list_tools(self) -> Sequence[MCPToolDescriptor]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...


class _SDKMCPClientSession:
    """Lazy wrapper around the official `mcp` Python SDK with persistent AsyncExitStack support."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._active_session: Any | None = None
        self.connect_count: int = 0

    async def _open_session_on_stack(self, stack: AsyncExitStack) -> Any:
        try:
            mcp_mod = importlib.import_module("mcp")
        except ImportError as exc:
            raise RunLatchError(
                "Optional dependency 'mcp' is required for live MCP server connections. "
                "Install it with: pip install 'runlatch[mcp]'"
            ) from exc

        client_session_cls = mcp_mod.ClientSession
        if self.config.transport == "stdio":
            if not self.config.command:
                raise RunLatchError("MCPServerConfig.command is required for stdio transport.")
            stdio_mod = importlib.import_module("mcp.client.stdio")
            params = stdio_mod.StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_mod.stdio_client(params)
            )
            session = await stack.enter_async_context(
                client_session_cls(read_stream, write_stream)
            )
            await session.initialize()
            self.connect_count += 1
            return session

        if not self.config.url:
            raise RunLatchError("MCPServerConfig.url is required for streamable_http transport.")
        http_mod = importlib.import_module("mcp.client.streamable_http")
        read_stream, write_stream, _ = await stack.enter_async_context(
            http_mod.streamablehttp_client(
                url=self.config.url, headers=self.config.headers or None
            )
        )
        session = await stack.enter_async_context(
            client_session_cls(read_stream, write_stream)
        )
        await session.initialize()
        self.connect_count += 1
        return session

    async def connect(self) -> None:
        """Open a persistent session via AsyncExitStack if not already connected."""
        if self._active_session is not None:
            return
        stack = AsyncExitStack()
        try:
            self._active_session = await self._open_session_on_stack(stack)
            self._stack = stack
        except Exception:
            await stack.aclose()
            raise

    async def close(self) -> None:
        """Close the persistent session and its underlying transport streams."""
        stack = self._stack
        self._stack = None
        self._active_session = None
        if stack is not None:
            await stack.aclose()

    async def _with_session(self, fn: Any) -> Any:
        if self._active_session is not None:
            return await fn(self._active_session)
        async with AsyncExitStack() as temp_stack:
            session = await self._open_session_on_stack(temp_stack)
            return await fn(session)

    async def list_tools(self) -> Sequence[MCPToolDescriptor]:
        async def _op(session: Any) -> list[MCPToolDescriptor]:
            res = await session.list_tools()
            descriptors: list[MCPToolDescriptor] = []
            for t in getattr(res, "tools", []):
                annot = getattr(t, "annotations", None)
                if annot is not None and hasattr(annot, "model_dump"):
                    annot_dict: dict[str, Any] = annot.model_dump()
                elif isinstance(annot, Mapping):
                    annot_dict = dict(annot)
                else:
                    annot_dict = {}
                descriptors.append(
                    MCPToolDescriptor(
                        name=str(t.name),
                        description=str(getattr(t, "description", "") or ""),
                        input_schema=dict(getattr(t, "inputSchema", None) or {"type": "object"}),
                        annotations=annot_dict,
                    )
                )
            return descriptors

        result: list[MCPToolDescriptor] = await self._with_session(_op)
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        async def _op(session: Any) -> str:
            res = await session.call_tool(name, arguments=arguments)
            parts: list[str] = []
            for block in getattr(res, "content", []):
                text = getattr(block, "text", None)
                parts.append(str(text if text is not None else block))
            return "\n".join(parts)

        output: str = await self._with_session(_op)
        return output


class MCPGovernedTool:
    """RegisteredTool wrapping a remote MCP tool with PolicyEngine and SecretRedactor."""

    def __init__(
        self,
        *,
        server_name: str,
        descriptor: MCPToolDescriptor,
        session: MCPClientSession,
        risk_tier: RiskTier,
        policy: PolicyEvaluator | None,
        redactor: SecretRedactorProtocol,
        timeout_s: float = 30.0,
        max_output_chars: int = 16000,
    ) -> None:
        self._server_name = server_name
        self._remote_name = descriptor.name
        self._session = session
        self._policy = policy
        self._redactor = redactor
        namespaced = f"{server_name}__{descriptor.name}"

        schema = dict(descriptor.input_schema) if descriptor.input_schema else {}
        if "type" not in schema:
            schema["type"] = "object"
        if "properties" not in schema:
            schema["properties"] = {}

        self._spec = ToolSpec(
            name=namespaced,
            description=(
                descriptor.description
                or f"MCP tool '{descriptor.name}' on server '{server_name}'."
            ),
            parameters=schema,
            risk_tier=risk_tier,
            requires_approval=(risk_tier == RiskTier.EXTERNAL),
            timeout_s=timeout_s,
            max_output_chars=max_output_chars,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def remote_name(self) -> str:
        return self._remote_name

    async def invoke(self, args: dict[str, Any]) -> str:
        """Enforce policy, pass live args to remote MCP server, and redact output."""
        if self._policy is not None:
            decision = self._policy.evaluate(self._spec, args)
            if decision.action == PolicyAction.DENY:
                raise PolicyDeniedError(self._spec.name, decision.reason)
            if decision.action == PolicyAction.REQUIRE_APPROVAL:
                raise ApprovalRequiredSignal(uuid4(), uuid4(), self._spec.name)

        raw_out = await asyncio.wait_for(
            self._session.call_tool(self._remote_name, args),
            timeout=self._spec.timeout_s,
        )
        redacted_out = self._redactor.redact_text(str(raw_out))
        return truncate_output(redacted_out, self._spec.max_output_chars)


class MCPToolkit:
    """Fail-closed governance bridge for Model Context Protocol (MCP) servers."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        session: MCPClientSession | None = None,
        default_risk: RiskTier = RiskTier.EXTERNAL,
        tool_risk_overrides: Mapping[str, RiskTier] | None = None,
        policy: PolicyEvaluator | None = None,
        redactor: SecretRedactorProtocol | None = None,
        enforce_policy_on_direct_invoke: bool = False,
    ) -> None:
        self.config = config
        self._session: MCPClientSession = session or _SDKMCPClientSession(config)
        self.default_risk = default_risk
        self.tool_risk_overrides: dict[str, RiskTier] = dict(tool_risk_overrides or {})
        self.policy: PolicyEvaluator = policy or PolicyEngine()
        self.redactor: SecretRedactorProtocol = redactor or SecretRedactor()
        self.enforce_policy_on_direct_invoke = enforce_policy_on_direct_invoke
        self._discovered_tools: list[MCPGovernedTool] = []
        self._exit_stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MCPToolkit:
        """Enter persistent MCP session context using AsyncExitStack."""
        stack = AsyncExitStack()
        try:
            connect_fn = getattr(self._session, "connect", None)
            close_fn = getattr(self._session, "close", None)
            if callable(connect_fn) and callable(close_fn):
                res = connect_fn()
                if asyncio.iscoroutine(res):
                    await res
                stack.push_async_callback(close_fn)
            elif hasattr(self._session, "__aenter__") and hasattr(self._session, "__aexit__"):
                await stack.enter_async_context(self._session)  # type: ignore[arg-type]
            self._exit_stack = stack
            return self
        except Exception:
            await stack.aclose()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc_val, exc_tb
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    def _resolve_risk_tier(self, descriptor: MCPToolDescriptor) -> RiskTier:
        """Resolve RiskTier fail-closed: exact match -> fnmatch glob -> default_risk."""
        namespaced = f"{self.config.server_name}__{descriptor.name}"

        # 1. Exact match wins over glob
        if namespaced in self.tool_risk_overrides:
            return self.tool_risk_overrides[namespaced]
        if descriptor.name in self.tool_risk_overrides:
            return self.tool_risk_overrides[descriptor.name]

        # 2. fnmatch glob support (e.g. "ops__get_*", "get_*", "*__read_*")
        for pattern, tier in self.tool_risk_overrides.items():
            if any(ch in pattern for ch in ("*", "?", "[")):
                if fnmatch.fnmatchcase(namespaced, pattern) or fnmatch.fnmatchcase(
                    descriptor.name, pattern
                ):
                    return tier

        # 3. Fail-closed invariant: NEVER trust descriptor.annotations.get("readOnlyHint")
        return self.default_risk

    async def discover_tools(self) -> list[RegisteredTool]:
        """Connect to the MCP server, discover tools, and wrap each with fail-closed governance."""
        descriptors = await self._session.list_tools()
        wrapped: list[MCPGovernedTool] = []
        for desc in descriptors:
            tier = self._resolve_risk_tier(desc)
            wrapped.append(
                MCPGovernedTool(
                    server_name=self.config.server_name,
                    descriptor=desc,
                    session=self._session,
                    risk_tier=tier,
                    policy=self.policy if self.enforce_policy_on_direct_invoke else None,
                    redactor=self.redactor,
                    timeout_s=self.config.timeout_s,
                )
            )
        self._discovered_tools = wrapped
        return list(wrapped)

    def tools(self) -> list[RegisteredTool]:
        """Return previously discovered tools."""
        return list(self._discovered_tools)
