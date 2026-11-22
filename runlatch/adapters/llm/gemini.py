"""Native Google Gemini REST `generateContent` adapter over async httpx.

Translates `ToolSpec` and `Message` transcripts into Gemini `functionDeclarations`,
`functionCall`, and `functionResponse` blocks with exponential backoff on HTTP 429/5xx.
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


def _strip_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of JSON schema without `additionalProperties` for Gemini compatibility."""
    return {k: v for k, v in schema.items() if k != "additionalProperties"}


class GeminiProvider:
    """Google Gemini REST API provider implementing LLMProvider."""

    def __init__(
        self,
        *,
        model: str = "gemini-2.0-flash",
        api_key: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 60.0,
        max_retries: int = 3,
        base_backoff_s: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._client = client

    @staticmethod
    def _build_contents(
        messages: list[Message],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        system_parts: list[dict[str, str]] = []
        contents: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append({"text": msg.content})
                continue

            if msg.role == "user":
                contents.append(
                    {"role": "user", "parts": [{"text": msg.content or ""}]}
                )
                continue

            if msg.role == "assistant":
                parts: list[dict[str, Any]] = []
                if msg.content:
                    parts.append({"text": msg.content})
                for tc in msg.tool_calls:
                    parts.append(
                        {
                            "functionCall": {
                                "name": tc.name,
                                "args": tc.arguments,
                            }
                        }
                    )
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                continue

            if msg.role == "tool":
                fn_resp_part = {
                    "functionResponse": {
                        "name": msg.name or "tool",
                        "response": {"result": msg.content or ""},
                    }
                }
                contents.append({"role": "user", "parts": [fn_resp_part]})

        sys_inst = {"parts": system_parts} if system_parts else None
        return sys_inst, contents

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        system_instruction, contents = self._build_contents(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction
        if tools:
            declarations = [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": _strip_additional_properties(t.parameters),
                }
                for t in tools
            ]
            body["tools"] = [{"functionDeclarations": declarations}]

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        url = f"{self.base_url}/models/{self.model}:generateContent"
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
                    raise RunLatchError(f"Gemini network request failed: {exc}") from exc

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
                        f"Gemini HTTP {resp.status_code} from {url}: {resp.text[:400]}"
                    )

                data = resp.json()
                latency_ms = int((time.perf_counter() - start) * 1000)
                return self._parse_response(data, latency_ms)

            raise RunLatchError(f"Gemini call exhausted retries: {last_error}")
        finally:
            if owns_client:
                await client.aclose()

    def _parse_response(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        candidates = data.get("candidates") or [{}]
        cand = candidates[0]
        content_obj = cand.get("content") or {}
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for part in content_obj.get("parts") or []:
            if "text" in part and part["text"]:
                text_parts.append(str(part["text"]))
            elif "functionCall" in part:
                fc = part["functionCall"] or {}
                raw_args = fc.get("args")
                args = raw_args if isinstance(raw_args, dict) else {}
                tool_calls.append(
                    ToolCall(
                        id=str(fc.get("id") or f"gemini_call_{len(tool_calls)}"),
                        name=str(fc.get("name") or ""),
                        arguments=args,
                    )
                )

        usage = data.get("usageMetadata") or {}
        in_tok = int(usage.get("promptTokenCount", 0))
        out_tok = int(usage.get("candidatesTokenCount", 0))
        cost = calculate_cost_usd(self.model, in_tok, out_tok)

        finish_reason: Literal["stop", "tool_calls", "length", "error"] = (
            "tool_calls" if tool_calls else "stop"
        )
        if cand.get("finishReason") == "MAX_TOKENS":
            finish_reason = "length"

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            model=self.model,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
