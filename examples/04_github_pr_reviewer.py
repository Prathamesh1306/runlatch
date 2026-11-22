"""Example 04: Running the built-in `pr-review` workflow."""

from __future__ import annotations

import asyncio

from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.workflows.pr_review import (
    build_pr_review_workflow,
    pr_review_demo_responses,
)


async def main() -> None:
    provider = MockLLMProvider(pr_review_demo_responses(), model="demo")
    workflow = build_pr_review_workflow(provider, workspace_root=".", max_cost_usd=0.15)
    result = await workflow.run({"pr_number": 42})
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
