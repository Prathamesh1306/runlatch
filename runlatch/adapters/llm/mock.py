"""Deterministic MockLLMProvider for $0 unit tests and --demo mode."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from runlatch.core.types import LLMResponse, Message, ToolSpec


class MockLLMProvider:
    """Scripted deterministic LLM provider requiring zero network or API keys."""

    def __init__(
        self,
        responses: Sequence[LLMResponse] | None = None,
        *,
        responder: Callable[[list[Message], list[ToolSpec] | None], LLMResponse] | None = None,
        default_content: str = "Task completed.",
        model: str = "mock",
    ) -> None:
        self._queue: deque[LLMResponse] = deque(responses or ())
        self._responder = responder
        self._default_content = default_content
        self._model = model
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, *responses: LLMResponse) -> None:
        """Append scripted responses to the queue."""
        for resp in responses:
            self._queue.append(resp)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools) if tools else None,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self._responder is not None:
            return self._responder(messages, tools)
        if self._queue:
            return self._queue.popleft()
        return LLMResponse(
            content=self._default_content,
            tool_calls=[],
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0,
            model=self._model,
            latency_ms=1,
            finish_reason="stop",
        )
