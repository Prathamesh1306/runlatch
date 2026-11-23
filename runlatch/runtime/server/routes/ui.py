"""Zero-build Jinja2 + HTMX Live Run Inspector (`/ui`) and read-only Approval Confirmation UI.

CRITICAL INVARIANT (AGENTS.md §3.7):
`GET /ui/approvals/{id}` is STRICTLY READ-ONLY and NEVER mutates database state,
protecting against Slack/Discord link unfurlers and email security pre-fetch scanners.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from runlatch.core.types import ApprovalStatus
from runlatch.runtime.server.auth import (
    build_signed_approval_url,
    compute_approval_signature,
    verify_approval_signature,
)
from runlatch.runtime.server.deps import ServerContext, get_ctx

router = APIRouter(prefix="/ui", tags=["UI"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(diff_text: str) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Parse a unified git diff into structured line dictionaries and summary stats."""
    if not diff_text or not diff_text.strip():
        return [], {"added": 0, "removed": 0, "files": 0}

    lines: list[dict[str, str]] = []
    old_ln: int | None = None
    new_ln: int | None = None
    added = 0
    removed = 0
    files = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            files += 1
            lines.append(
                {
                    "kind": "meta",
                    "old_num": "",
                    "new_num": "",
                    "sign": "",
                    "text": raw,
                }
            )
        elif raw.startswith(("index ", "--- ", "+++ ", "new file mode", "deleted file mode")):
            lines.append(
                {
                    "kind": "meta",
                    "old_num": "",
                    "new_num": "",
                    "sign": "",
                    "text": raw,
                }
            )
        elif raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if match:
                old_ln = int(match.group(1))
                new_ln = int(match.group(2))
            lines.append(
                {
                    "kind": "hunk",
                    "old_num": "···",
                    "new_num": "···",
                    "sign": "@@",
                    "text": raw,
                }
            )
        elif raw.startswith("+"):
            added += 1
            num_str = str(new_ln) if new_ln is not None else "+"
            if new_ln is not None:
                new_ln += 1
            lines.append(
                {
                    "kind": "add",
                    "old_num": "",
                    "new_num": num_str,
                    "sign": "+",
                    "text": raw[1:] if len(raw) > 1 else "",
                }
            )
        elif raw.startswith("-"):
            removed += 1
            num_str = str(old_ln) if old_ln is not None else "-"
            if old_ln is not None:
                old_ln += 1
            lines.append(
                {
                    "kind": "del",
                    "old_num": num_str,
                    "new_num": "",
                    "sign": "-",
                    "text": raw[1:] if len(raw) > 1 else "",
                }
            )
        else:
            old_str = str(old_ln) if old_ln is not None else ""
            new_str = str(new_ln) if new_ln is not None else ""
            if old_ln is not None:
                old_ln += 1
            if new_ln is not None:
                new_ln += 1
            lines.append(
                {
                    "kind": "ctx",
                    "old_num": old_str,
                    "new_num": new_str,
                    "sign": " ",
                    "text": raw[1:] if raw.startswith(" ") else raw,
                }
            )

    if files == 0 and lines:
        files = 1
    return lines, {"added": added, "removed": removed, "files": files}


def _truncate_args_preview(args: dict[str, Any], max_len: int = 88) -> str:
    """Format a compact single-line JSON preview of tool arguments."""
    try:
        compact = json.dumps(args, ensure_ascii=False, separators=(", ", ": "))
    except TypeError:
        compact = str(args)
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1] + "…"


@router.get("", response_class=HTMLResponse)
async def live_run_inspector(
    request: Request,
    ctx: Annotated[ServerContext, Depends(get_ctx)],
) -> HTMLResponse:
    """Render the zero-build Jinja2 + HTMX Live Run Inspector dashboard."""
    runs = await ctx.store.list_checkpoints(limit=25)
    approvals = await ctx.store.list_approvals(status=ApprovalStatus.PENDING)
    summary = await ctx.store.get_metrics_summary()

    approval_links: dict[str, str] = {}
    approval_arg_previews: dict[str, str] = {}
    for item in approvals:
        key = str(item.id)
        approval_links[key] = build_signed_approval_url(
            "",
            item.id,
            ctx.settings.approval_signing_secret,
        )
        approval_arg_previews[key] = _truncate_args_preview(item.sanitized_args)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "runs": runs,
            "approvals": approvals,
            "approval_links": approval_links,
            "approval_arg_previews": approval_arg_previews,
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
        resolved_sig = sig or ""
        resolved_exp = exp if exp is not None else ""
    else:
        # Local dashboard convenience: issue a read-only in-memory token for the form
        generated_exp = int(time.time()) + 3600
        resolved_exp = generated_exp
        resolved_sig = compute_approval_signature(
            approval_id, generated_exp, ctx.settings.approval_signing_secret
        )

    approval = await ctx.store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Approval {approval_id} not found.",
        )

    checkpoint = await ctx.store.get_checkpoint(approval.run_id)
    diff_lines, diff_stats = parse_unified_diff(approval.diff_preview or "")
    return templates.TemplateResponse(
        request=request,
        name="approval_confirm.html",
        context={
            "approval": approval,
            "checkpoint": checkpoint,
            "diff_lines": diff_lines,
            "diff_stats": diff_stats,
            "sig": resolved_sig,
            "exp": resolved_exp,
        },
    )
