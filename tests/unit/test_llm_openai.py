"""Unit tests for OpenAICompatibleProvider presets, tool calling, and retry backoff."""

from __future__ import annotations

import json

import httpx
import pytest

from runlatch.adapters.llm.openai_compat import OpenAICompatibleProvider
from runlatch.core.types import Message, RiskTier, ToolSpec


@pytest.mark.asyncio
async def test_zero_cost_presets() -> None:
    ollama = OpenAICompatibleProvider.ollama()
    assert ollama.base_url == "http://localhost:11434/v1"
    assert ollama.model == "qwen2.5-coder:7b"

    groq = OpenAICompatibleProvider.groq(api_key="gsk_test")
    assert groq.base_url == "https://api.groq.com/openai/v1"
    assert groq.api_key == "gsk_test"

    gh = OpenAICompatibleProvider.github_models(token="ghp_test")
    assert gh.base_url == "https://models.inference.ai.azure.com"
    assert gh.api_key == "ghp_test"


@pytest.mark.asyncio
async def test_openai_provider_tool_call_and_retry_on_429() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content.decode())
        assert body["model"] == "gpt-4o-mini"
        assert body["tools"][0]["function"]["name"] == "read_file"

        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="Rate limited")

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "README.md"}),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 25},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            api_key="sk-test",
            base_backoff_s=0.01,
            client=client,
        )
        spec = ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            risk_tier=RiskTier.READ,
        )
        resp = await provider.complete(
            messages=[Message(role="user", content="Read README.md")],
            tools=[spec],
        )

    assert attempts == 2
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "README.md"}
    assert resp.input_tokens == 100
    assert resp.output_tokens == 25
    assert resp.cost_usd > 0.0
