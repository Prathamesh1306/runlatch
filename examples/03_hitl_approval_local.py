"""Example 03: Local Human-in-the-Loop Pause & Resume with SQLite Checkpoint."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from runlatch import SQLAlchemyCheckpointStore
from runlatch.adapters.llm.mock import MockLLMProvider
from runlatch.workflows.issue_to_pr import (
    build_issue_to_pr_workflow,
    issue_to_pr_demo_responses,
)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{Path(tmp) / 'demo_hitl.db'}"
        store = SQLAlchemyCheckpointStore(db_url)
        responses = issue_to_pr_demo_responses(108)

        # Phase 1: Run until HITL pause on RiskTier.EXTERNAL tool (github_create_pr)
        wf1 = build_issue_to_pr_workflow(
            MockLLMProvider(responses[:3]),
            workspace_root=".",
            store=store,
        )
        paused = await wf1.run({"issue_number": 108})
        print(f"Phase 1 Status: {paused.status} (Approval ID: {paused.approval_id})")

        # Phase 2: Human approves -> resume from SQLite checkpoint on fresh Workflow instance
        wf2 = build_issue_to_pr_workflow(
            MockLLMProvider([responses[3]]),
            workspace_root=".",
            store=store,
        )
        resumed = await wf2.resume(
            paused.run_id,
            approved=True,
            decided_by="prathamesh",
            reason="Verified diff and tests",
        )
        print(f"Phase 2 Status: {resumed.status}")
        print(f"Final Output: {resumed.output}")
        await store.dispose()


if __name__ == "__main__":
    asyncio.run(main())
