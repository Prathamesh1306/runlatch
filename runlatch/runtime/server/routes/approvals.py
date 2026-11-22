"""HITL approval listing and atomic `POST /api/v1/approvals/{id}/decision` endpoints."""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel

from runlatch.core.exceptions import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    RunLatchError,
)
from runlatch.core.types import ApprovalStatus, RunStatus
from runlatch.runtime.server.auth import verify_approval_signature
from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/api/v1/approvals", tags=["Approvals"])


class ApprovalDecisionRequest(BaseModel):
    """Payload for `POST /api/v1/approvals/{id}/decision`."""

    decision: Literal["approve", "reject"]
    reason: str = ""
    actor: str = "reviewer"
    sig: str | None = None
    exp: int | None = None


@router.get("")
async def list_approvals(
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    approval_status: Annotated[str | None, Query(alias="status")] = None,
    run_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """List HITL approval requests."""
    filter_status = ApprovalStatus(approval_status.upper()) if approval_status else None
    items = await ctx.store.list_approvals(status=filter_status, run_id=run_id)
    return [item.model_dump(mode="json") for item in items]


@router.post("/{approval_id}/decision")
async def decide_approval_endpoint(
    approval_id: UUID,
    request: Request,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
    authorization: str | None = Header(default=None),
    sig: str | None = Query(default=None),
    exp: int | None = Query(default=None),
) -> dict[str, Any]:
    """Approve or reject a paused tool call with atomic single-winner locking.

    Supports both JSON API clients (`application/json`) and HTML form submissions
    from the HTMX confirmation page (`application/x-www-form-urlencoded`).
    """
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        raw_dec = str(form.get("decision", "")).lower()
        if raw_dec not in ("approve", "reject"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="decision must be 'approve' or 'reject'.",
            )
        req = ApprovalDecisionRequest(
            decision="approve" if raw_dec == "approve" else "reject",
            reason=str(form.get("reason", "")),
            actor=str(form.get("actor", "web-ui")),
            sig=str(form.get("sig")) if form.get("sig") else sig,
            exp=int(str(form.get("exp"))) if form.get("exp") else exp,
        )
    else:
        body = await request.json()
        req = ApprovalDecisionRequest.model_validate(body)
        if req.sig is None:
            req.sig = sig
        if req.exp is None:
            req.exp = exp

    # Authenticate via either Bearer API key OR valid signed approval token (sig + exp)
    bearer_ok = False
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        bearer_ok = bool(token) and hmac.compare_digest(token, ctx.settings.api_key)

    if not bearer_ok:
        sig_ok, err_code = verify_approval_signature(
            approval_id, req.sig, req.exp, ctx.settings.approval_signing_secret
        )
        if not sig_ok:
            if err_code == "expired_signature":
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="Signed approval token has expired.",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid Bearer token or signed approval token required.",
            )

    lock = ctx.get_approval_lock(approval_id)
    async with lock:
        try:
            updated = await ctx.store.decide_approval(
                approval_id,
                approved=(req.decision == "approve"),
                decided_by=req.actor,
                reason=req.reason,
            )
        except ApprovalAlreadyDecidedError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ApprovalExpiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=str(exc),
            ) from exc
        except RunLatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        cp = await ctx.store.get_checkpoint(updated.run_id)
        if cp is not None and cp.status == RunStatus.WAITING_APPROVAL:
            cp.status = RunStatus.RESUMING
            await ctx.store.save_checkpoint(cp)

        task_id = await ctx.dispatcher.dispatch_resume(updated.run_id, approval_id)

    return {
        "approval_id": str(updated.id),
        "run_id": str(updated.run_id),
        "status": updated.status.value,
        "decided_by": updated.decided_by,
        "decision_reason": updated.decision_reason,
        "resume_task_id": task_id,
    }
