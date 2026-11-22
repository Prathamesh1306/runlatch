"""Token cost pricing table for LLM providers (USD per 1M tokens)."""

from __future__ import annotations

# (input_usd_per_1m, output_usd_per_1m)
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    # Zero-cost local / mock / free presets
    "mock": (0.0, 0.0),
    "demo": (0.0, 0.0),
    "qwen2.5-coder:7b": (0.0, 0.0),
    "llama3.1": (0.0, 0.0),
    "llama3.1:8b": (0.0, 0.0),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Groq
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    # Anthropic
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-3-7-sonnet-latest": (3.00, 15.00),
    # Gemini
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-pro": (1.25, 10.00),
}

DEFAULT_PRICING_PER_1M: tuple[float, float] = (0.50, 1.50)


def calculate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate exact USD cost for a single LLM call."""
    normalized = model.strip().lower()
    if normalized.startswith("ollama/") or ":" in normalized and "claude" not in normalized:
        pricing = MODEL_PRICING_PER_1M.get(normalized, (0.0, 0.0))
    else:
        pricing = MODEL_PRICING_PER_1M.get(normalized, DEFAULT_PRICING_PER_1M)
    in_rate, out_rate = pricing
    cost = (max(input_tokens, 0) * in_rate + max(output_tokens, 0) * out_rate) / 1_000_000.0
    return round(cost, 8)
