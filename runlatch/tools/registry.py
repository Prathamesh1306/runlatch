"""Tool registration, Pydantic JSON Schema compilation, and output truncation.

Imports ONLY Python stdlib, pydantic, and runlatch.core.*.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Sequence
from typing import Any, get_type_hints

from pydantic import BaseModel, Field, ValidationError, create_model

from runlatch.core.protocols import RegisteredTool
from runlatch.core.types import RiskTier, ToolSpec


def truncate_output(text: str, max_chars: int = 16000) -> str:
    """Head+tail truncate tool output exceeding max_chars."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    removed = len(text) - max_chars
    marker = f"\n... [truncated {removed} chars] ...\n"
    if max_chars <= len(marker) + 20:
        return text[:max_chars]
    budget = max_chars - len(marker)
    head = budget // 2
    tail = budget - head
    return f"{text[:head]}{marker}{text[-tail:]}"


def _compile_args_model(fn: Callable[..., Any], tool_name: str) -> type[BaseModel]:
    """Build a Pydantic v2 BaseModel from a function's type annotations and defaults."""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    fields: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = Any
        if param.default is inspect.Parameter.empty:
            fields[param_name] = (annotation, Field(...))
        else:
            fields[param_name] = (annotation, Field(default=param.default))

    model_name = f"{tool_name.title().replace('_', '')}Args"
    return create_model(model_name, **fields)


def _clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a clean OpenAI/Anthropic-compatible JSON Schema parameters object."""
    return {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
        "additionalProperties": False,
    }


class FunctionTool:
    """Concrete RegisteredTool wrapping a typed sync or async Python function."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        risk: RiskTier = RiskTier.READ,
        requires_approval: bool = False,
        timeout_s: float = 30.0,
        max_output_chars: int = 16000,
    ) -> None:
        self._fn = fn
        self._is_coro = inspect.iscoroutinefunction(fn)
        tool_name = name or fn.__name__
        doc = (description or inspect.getdoc(fn) or f"Execute {tool_name}.").strip()
        self._args_model = _compile_args_model(fn, tool_name)
        raw_schema = self._args_model.model_json_schema()
        self._spec = ToolSpec(
            name=tool_name,
            description=doc,
            parameters=_clean_json_schema(raw_schema),
            risk_tier=risk,
            requires_approval=requires_approval or (risk == RiskTier.EXTERNAL),
            timeout_s=timeout_s,
            max_output_chars=max_output_chars,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate raw LLM tool arguments against the compiled Pydantic schema."""
        validated = self._args_model.model_validate(args)
        return {k: getattr(validated, k) for k in self._args_model.model_fields}

    async def invoke(self, args: dict[str, Any]) -> str:
        """Validate arguments, invoke the function with timeout, and truncate output.

        If Pydantic argument validation fails, returns a structured validation error
        message instead of crashing so the LLM can self-correct on its next turn.
        """
        try:
            clean_kwargs = self.validate_arguments(args)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            return f"ToolValidationError: Invalid arguments for '{self._spec.name}': {errors}"

        async def _call() -> Any:
            if self._is_coro:
                return await self._fn(**clean_kwargs)
            return await asyncio.to_thread(self._fn, **clean_kwargs)

        raw_result = await asyncio.wait_for(_call(), timeout=self._spec.timeout_s)

        if isinstance(raw_result, str):
            text = raw_result
        elif isinstance(raw_result, BaseModel):
            text = raw_result.model_dump_json(indent=2)
        elif isinstance(raw_result, (dict, list)):
            text = json.dumps(raw_result, indent=2, default=str)
        else:
            text = str(raw_result)

        return truncate_output(text, self._spec.max_output_chars)


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    risk: RiskTier = RiskTier.READ,
    requires_approval: bool = False,
    timeout_s: float = 30.0,
    max_output_chars: int = 16000,
) -> Any:
    """Decorator compiling a typed Python function into a RegisteredTool."""

    def _decorator(target: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            risk=risk,
            requires_approval=requires_approval,
            timeout_s=timeout_s,
            max_output_chars=max_output_chars,
        )

    if fn is not None:
        return _decorator(fn)
    return _decorator


class ToolRegistry:
    """Lookup table of RegisteredTool instances for an Agent."""

    def __init__(self, tools: Sequence[RegisteredTool] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for t in tools:
            self.register(t)

    def register(self, item: RegisteredTool | Callable[..., Any]) -> RegisteredTool:
        wrapped: RegisteredTool = (
            item if isinstance(item, RegisteredTool) else FunctionTool(item)
        )
        self._tools[wrapped.spec.name] = wrapped
        return wrapped

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
