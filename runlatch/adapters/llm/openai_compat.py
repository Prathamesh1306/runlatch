"""OpenAI-compatible Chat Completions adapter over async httpx.

Supports OpenAI, Ollama, Groq, GitHub Models, Azure OpenAI, and OpenRouter.
Includes 1-line zero-cost presets (.ollama(), .groq(), .github_models()) and
exponential backoff retry on HTTP 429/5xx.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Literal

import httpx

from runlatch.adapters.llm.pricing import calculate_cost_usd
from runlatch.core.exceptions import RunLatchError
from runlatch.core.types import LLMResponse, Message, ToolCall, ToolSpec

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


class OpenAICompatibleProvider:
    """Unified OpenAI-compatible Chat Completions LLM provider."""

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
        max_retries: int = 3,
        base_backoff_s: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "ollama")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._client = client

    @classmethod
    def ollama(
        cls,
        model: str = "qwen2.5-coder:7b",
        *,
        base_url: str = "http://localhost:11434/v1",
        client: httpx.AsyncClient | None = None,
    ) -> OpenAICompatibleProvider:
        """1-line zero-cost local Ollama preset."""
        return cls(
            model=model,
            api_key="ollama",
            base_url=base_url,
            client=client,
        )

    @classmethod
    def groq(
        cls,
        api_key: str | None = None,
        *,
        model: str = "llama-3.3-70b-versatile",
        client: httpx.AsyncClient | None = None,
    ) -> OpenAICompatibleProvider:
        """1-line Groq free-tier cloud preset."""
        resolved_key = api_key or os.environ.get("GROQ_API_KEY", "")
        return cls(
            model=model,
            api_key=resolved_key,
            base_url="https://api.groq.com/openai/v1",
            client=client,
        )

    @classmethod
    def github_models(
        cls,
        token: str | None = None,
        *,
        model: str = "gpt-4o-mini",
        client: httpx.AsyncClient | None = None,
    ) -> OpenAICompatibleProvider:
        """1-line GitHub Models free-tier preset using GITHUB_TOKEN."""
        resolved_token = token or os.environ.get("GITHUB_TOKEN", "")
        return cls(
            model=model,
            api_key=resolved_token,
            base_url="https://models.inference.ai.azure.com",
            client=client,
        )

    @staticmethod
    def _serialize_message(msg: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": msg.role}
        if msg.content is not None or not msg.tool_calls:
            payload["content"] = msg.content or ""
        else:
            payload["content"] = None
        if msg.name:
            payload["name"] = msg.name
        if msg.tool_call_id:
            payload["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in msg.tool_calls
            ]
        return payload

    @staticmethod
    def _serialize_tool(spec: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [self._serialize_tool(t) for t in tools]
            body["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
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
                    raise RunLatchError(f"LLM network request failed: {exc}") from exc

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
                        f"LLM HTTP {resp.status_code} from {url}: {resp.text[:400]}"
                    )

                data = resp.json()
                latency_ms = int((time.perf_counter() - start) * 1000)
                return self._parse_response(data, latency_ms)

            raise RunLatchError(f"LLM call exhausted retries: {last_error}")
        finally:
            if owns_client:
                await client.aclose()

    def _parse_response(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        choices = data.get("choices") or [{}]
        choice = choices[0]
        msg = choice.get("message") or {}
        content: str | None = msg.get("content")

        parsed_calls: list[ToolCall] = []
        for raw_tc in msg.get("tool_calls") or []:
            fn_block = raw_tc.get("function") or {}
            raw_args = fn_block.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    parsed_args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            parsed_calls.append(
                ToolCall(
                    id=str(raw_tc.get("id") or f"call_{len(parsed_calls)}"),
                    name=str(fn_block.get("name") or ""),
                    arguments=parsed_args if isinstance(parsed_args, dict) else {},
                )
            )

        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        model_used = str(data.get("model") or self.model)

        cost = calculate_cost_usd(model_used, in_tok, out_tok)
        finish_reason: Literal["stop", "tool_calls", "length", "error"] = (
            "tool_calls" if parsed_calls else "stop"
        )
        raw_finish = choice.get("finish_reason")
        if raw_finish == "length":
            finish_reason = "length"

        return LLMResponse(
            content=content,
            tool_calls=parsed_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            model=model_used,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
