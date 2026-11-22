"""Authentication, GitHub HMAC-SHA256 verification, and signed approval URL helpers."""

from __future__ import annotations

import hashlib
import hmac
import time
from uuid import UUID

from fastapi import Header, HTTPException, Request, status


def compute_github_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute `sha256=<hex>` GitHub webhook signature."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_signature(
    payload_bytes: bytes, signature_header: str | None, secret: str
) -> bool:
    """Constant-time verification of `X-Hub-Signature-256`."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = compute_github_signature(payload_bytes, secret)
    return hmac.compare_digest(signature_header.strip(), expected)


def compute_approval_signature(approval_id: UUID, exp: int, secret: str) -> str:
    """Compute HMAC-SHA256 signature over `{approval_id}:{exp}`."""
    msg = f"{approval_id}:{exp}".encode()
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_signed_approval_url(
    base_url: str,
    approval_id: UUID,
    secret: str,
    *,
    ttl_minutes: int = 60,
    now_ts: int | None = None,
) -> str:
    """Generate a read-only `GET /ui/approvals/{id}?sig=...&exp=...` confirmation URL."""
    base = base_url.rstrip("/")
    current = now_ts if now_ts is not None else int(time.time())
    exp = current + (ttl_minutes * 60)
    sig = compute_approval_signature(approval_id, exp, secret)
    return f"{base}/ui/approvals/{approval_id}?sig={sig}&exp={exp}"


def verify_approval_signature(
    approval_id: UUID,
    sig: str | None,
    exp: int | None,
    secret: str,
    *,
    now_ts: int | None = None,
) -> tuple[bool, str]:
    """Verify signed approval query parameters. Returns `(is_valid, error_code)`."""
    if not sig or exp is None:
        return False, "missing_signature"
    expected = compute_approval_signature(approval_id, exp, secret)
    if not hmac.compare_digest(sig.strip(), expected):
        return False, "invalid_signature"
    current = now_ts if now_ts is not None else int(time.time())
    if current > exp:
        return False, "expired_signature"
    return True, "ok"


async def require_bearer_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency enforcing constant-time Bearer API key authentication."""
    ctx = request.app.state.ctx
    expected_key: str = ctx.settings.api_key
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization: Bearer header.",
        )
    token = authorization[7:].strip()
    if not token or not hmac.compare_digest(token, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    return token
