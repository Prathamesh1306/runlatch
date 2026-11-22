"""Liveness and readiness health probe endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(tags=["Health"])


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    """Liveness probe confirming the server process is responsive."""
    return {"status": "alive"}


@router.get("/health/ready")
async def health_ready(
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> dict[str, str]:
    """Readiness probe verifying database and idempotency store connectivity."""
    try:
        await ctx.store.initialize()
        idem_ok = await ctx.idempotency.ping()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Dependency not ready: {exc}",
        ) from exc

    if not idem_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis idempotency backend unreachable.",
        )
    return {"status": "ready", "database": "ok", "idempotency": "ok"}
