"""Zero-build Jinja2 + HTMX Live Run Inspector (`/ui`) and read-only Approval Confirmation UI.

CRITICAL INVARIANT (AGENTS.md §3.7):
`GET /ui/approvals/{id}` is STRICTLY READ-ONLY and NEVER mutates database state,
protecting against Slack/Discord link unfurlers and email security pre-fetch scanners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from runlatch.core.types import ApprovalStatus
from runlatch.runtime.server.auth import verify_approval_signature
from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/ui", tags=["UI"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def live_run_inspector(
    request: Request,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> HTMLResponse:
    """Render the zero-build Jinja2 + HTMX Live Run Inspector dashboard."""
    runs = await ctx.store.list_checkpoints(limit=25)
    approvals = await ctx.store.list_approvals(status=ApprovalStatus.PENDING)
    summary = await ctx.store.get_metrics_summary()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "runs": runs,
            "approvals": approvals,
            "summary": summary,
        },
    )


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def approval_confirmation_page(
    approval_id: UUID,
    request: Request,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    sig: str | None = Query(default=None),
    exp: int | None = Query(default=None),
) -> HTMLResponse:
    """Render a read-only HITL confirmation page. NEVER mutates DB state on GET."""
    if sig is not None or exp is not None:
        valid, err_code = verify_approval_signature(
            approval_id, sig, exp, ctx.settings.approval_signing_secret
        )
        if not valid:
            code = (
                status.HTTP_410_GONE
                if err_code == "expired_signature"
                else status.HTTP_401_UNAUTHORIZED
            )
            raise HTTPException(
                status_code=code, detail=f"Invalid or expired approval link ({err_code})."
            )

    approval = await ctx.store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Approval {approval_id} not found.",
        )

    checkpoint = await ctx.store.get_checkpoint(approval.run_id)
    return templates.TemplateResponse(
        request=request,
        name="approval_confirm.html",
        context={
            "approval": approval,
            "checkpoint": checkpoint,
            "sig": sig or "",
            "exp": exp or "",
        },
    )
