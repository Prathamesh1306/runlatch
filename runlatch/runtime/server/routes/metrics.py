"""Execution, token, and USD cost metrics endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/api/v1/metrics", tags=["Metrics"])


@router.get("/summary")
async def metrics_summary(
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> dict[str, Any]:
    """Return aggregated runs, success rate, token usage, and USD cost by workflow."""
    return await ctx.store.get_metrics_summary()
