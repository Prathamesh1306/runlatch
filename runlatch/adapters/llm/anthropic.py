"""Native Anthropic Claude Messages API adapter over async httpx.

Translates `ToolSpec` and `Message` transcripts into Anthropic `tool_use` and
`tool_result` blocks with exponential backoff on HTTP 429/5xx/529.
Imports ONLY Python stdlib, httpx, runlatch.core.*, and runlatch.adapters.llm.pricing.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Literal

import httpx

from runlatch.adapters.llm.pricing import calculate_cost_usd
from runlatch.core.exceptions import RunLatchError
from runlatch.core.types import LLMResponse, Message, ToolCall, ToolSpec

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


class AnthropicProvider:
    """Anthropic Claude Messages API provider implementing LLMProvider."""

    def __init__(
        self,
        *,
        model: str = "claude-3-5-sonnet-latest",
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        timeout_s: float = 60.0,
        max_retries: int = 3,
        base_backoff_s: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.anthropic_version = anthropic_version
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._client = client

    @staticmethod
    def _serialize_tool(spec: ToolSpec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }

    @staticmethod
    def _build_messages(
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        convo: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
                continue

            if msg.role == "user":
                convo.append({"role": "user", "content": msg.content or ""})
                continue

            if msg.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                convo.append({"role": "assistant", "content": blocks or ""})
                continue

            if msg.role == "tool":
                tool_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content or "",
                }
                if (
                    convo
                    and convo[-1]["role"] == "user"
                    and isinstance(convo[-1]["content"], list)
                ):
                    convo[-1]["content"].append(tool_block)
                else:
                    convo.append({"role": "user", "content": [tool_block]})

        system_prompt = "\n\n".join(system_parts) if system_parts else None
        return system_prompt, convo

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        system_prompt, anthropic_msgs = self._build_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            body["system"] = system_prompt
        if tools:
            body["tools"] = [self._serialize_tool(t) for t in tools]

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }
        url = f"{self.base_url}/v1/messages"
        start = time.perf_counter()

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            last_error: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(url, json=body, headers=headers)
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.base_backoff_s * (2**attempt))
                        continue
                    raise RunLatchError(f"Anthropic network request failed: {exc}") from exc

                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else self.base_backoff_s * (2**attempt)
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    raise RunLatchError(
                        f"Anthropic HTTP {resp.status_code} from {url}: {resp.text[:400]}"
                    )

                data = resp.json()
                latency_ms = int((time.perf_counter() - start) * 1000)
                return self._parse_response(data, latency_ms)

            raise RunLatchError(f"Anthropic call exhausted retries: {last_error}")
        finally:
            if owns_client:
                await client.aclose()

    def _parse_response(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in data.get("content") or []:
            b_type = block.get("type")
            if b_type == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
            elif b_type == "tool_use":
                raw_in = block.get("input")
                args = raw_in if isinstance(raw_in, dict) else {}
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"toolu_{len(tool_calls)}"),
                        name=str(block.get("name") or ""),
                        arguments=args,
                    )
                )

        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        model_used = str(data.get("model") or self.model)
        cost = calculate_cost_usd(model_used, in_tok, out_tok)

        finish_reason: Literal["stop", "tool_calls", "length", "error"] = (
            "tool_calls" if tool_calls else "stop"
        )
        if data.get("stop_reason") == "max_tokens":
            finish_reason = "length"

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            model=model_used,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
