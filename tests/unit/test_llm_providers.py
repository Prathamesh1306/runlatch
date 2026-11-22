"""Unit tests for AnthropicProvider, GeminiProvider, and OpenTelemetrySubscriber."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from runlatch.adapters.llm.anthropic import AnthropicProvider
from runlatch.adapters.llm.gemini import GeminiProvider
from runlatch.adapters.observability.otel import OpenTelemetrySubscriber
from runlatch.core.events import EventBus
from runlatch.core.types import (
    LifecycleEvent,
    Message,
    RiskTier,
    ToolCall,
    ToolSpec,
)


@pytest.mark.asyncio
async def test_anthropic_provider_tool_use_tool_result_and_529_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers["x-api-key"] == "sk-ant-test"
        body = json.loads(request.content.decode("utf-8"))
        assert body["system"] == "You are a reviewer."
        assert body["tools"][0]["name"] == "read_file"
        assert "input_schema" in body["tools"][0]

        if attempts == 1:
            return httpx.Response(
                529, headers={"Retry-After": "0.01"}, text="Overloaded"
            )

        return httpx.Response(
            200,
            json={
                "id": "msg_01",
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Checking README.md"},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    },
                ],
                "usage": {"input_tokens": 120, "output_tokens": 30},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = AnthropicProvider(
            model="claude-3-5-sonnet-latest",
            api_key="sk-ant-test",
            base_backoff_s=0.01,
            client=client,
        )
        spec = ToolSpec(
            name="read_file",
            description="Read file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            risk_tier=RiskTier.READ,
        )
        resp = await provider.complete(
            messages=[
                Message(role="system", content="You are a reviewer."),
                Message(role="user", content="Read README.md"),
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(id="toolu_0", name="read_file", arguments={"path": "a.py"})
                    ],
                ),
                Message(
                    role="tool",
                    tool_call_id="toolu_0",
                    name="read_file",
                    content="x = 1",
                ),
            ],
            tools=[spec],
        )

    assert attempts == 2
    assert resp.finish_reason == "tool_calls"
    assert resp.content == "Checking README.md"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "toolu_123"
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "README.md"}
    assert resp.input_tokens == 120
    assert resp.output_tokens == 30
    assert resp.cost_usd > 0.0


@pytest.mark.asyncio
async def test_gemini_provider_function_declarations_and_retry_on_429() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers["x-goog-api-key"] == "gemini-test-key"
        body = json.loads(request.content.decode("utf-8"))
        assert body["systemInstruction"]["parts"][0]["text"] == "System rules"
        fn_decl = body["tools"][0]["functionDeclarations"][0]
        assert fn_decl["name"] == "git_diff"
        assert "additionalProperties" not in fn_decl["parameters"]

        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="Quota")

        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "git_diff",
                                        "args": {"max_bytes": 1024},
                                    }
                                }
                            ],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 90,
                    "candidatesTokenCount": 20,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            model="gemini-2.0-flash",
            api_key="gemini-test-key",
            base_backoff_s=0.01,
            client=client,
        )
        spec = ToolSpec(
            name="git_diff",
            description="Diff against HEAD",
            parameters={
                "type": "object",
                "properties": {"max_bytes": {"type": "integer"}},
                "additionalProperties": False,
            },
            risk_tier=RiskTier.READ,
        )
        resp = await provider.complete(
            messages=[
                Message(role="system", content="System rules"),
                Message(role="user", content="Run git_diff"),
            ],
            tools=[spec],
        )

    assert attempts == 2
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "git_diff"
    assert resp.tool_calls[0].arguments == {"max_bytes": 1024}
    assert resp.input_tokens == 90
    assert resp.output_tokens == 20
    assert resp.cost_usd > 0.0


@pytest.mark.asyncio
async def test_opentelemetry_subscriber_records_spans_and_handles_missing_otel() -> None:
    emitted_spans: list[tuple[str, dict[str, Any]]] = []

    class FakeSpan:
        def __init__(self, name: str) -> None:
            self.name = name
            self.attrs: dict[str, Any] = {}

        def __enter__(self) -> FakeSpan:
            return self

        def __exit__(self, *args: object) -> None:
            emitted_spans.append((self.name, self.attrs))

        def set_attribute(self, key: str, value: Any) -> None:
            self.attrs[key] = value

    class FakeTracer:
        def start_as_current_span(self, name: str) -> FakeSpan:
            return FakeSpan(name)

    bus = EventBus()
    sub = OpenTelemetrySubscriber(tracer=FakeTracer()).attach(bus)
    run_id = uuid4()

    await bus.emit(
        LifecycleEvent(
            event_type="workflow.started",
            run_id=run_id,
            workflow_name="pr-review",
        )
    )
    await bus.emit(
        LifecycleEvent(
            event_type="tool.completed",
            run_id=run_id,
            workflow_name="pr-review",
            step_name="gather_context",
            payload={"tool": "git_diff", "status": "SUCCEEDED"},
        )
    )

    assert len(sub.recorded_spans) == 2
    assert sub.recorded_spans[0].name == "workflow.run"
    assert sub.recorded_spans[1].name == "tool.execute"
    assert sub.recorded_spans[1].attributes["runlatch.tool"] == "git_diff"
    assert len(emitted_spans) == 2

    # Also verify graceful no-op when tracer is None (OTel not installed)
    noop_sub = OpenTelemetrySubscriber(tracer=None)
    noop_sub.on_event(
        LifecycleEvent(
            event_type="run.completed",
            run_id=run_id,
            workflow_name="pr-review",
            payload={"tokens": 100, "cost_usd": 0.01},
        )
    )
    assert len(noop_sub.recorded_spans) == 1
