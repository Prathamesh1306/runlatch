"""Example 01: Minimal 15-line local tool-calling agent."""

from __future__ import annotations

import asyncio

from runlatch import Agent, FileToolkit, GitToolkit, MockLLMProvider
from runlatch.workflows.pr_review import pr_review_demo_responses


async def main() -> None:
    agent = Agent(
        name="git-summarizer",
        # Swap MockLLMProvider for OpenAICompatibleProvider.ollama() or .groq() for live LLMs
        provider=MockLLMProvider(pr_review_demo_responses()[:2]),
        tools=[*GitToolkit(".").tools(), *FileToolkit(".").tools()],
        max_cost_usd=0.05,
    )
    result = await agent.run("Inspect my git status and summarize the repository files.")
    print(result.output)
    print(f"Tokens: {result.total_tokens} | Cost: ${result.total_cost_usd:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
