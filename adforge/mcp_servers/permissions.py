"""MCP tool permissions — read-only vs write-operation decorators.

Every MCP tool MUST be decorated with either `@readonly` or `@write_operation`.
The `mcp.tool()` decorator from FastMCP goes on top of these.

Usage:
    @mcp.tool()
    @readonly
    async def web_search(query, headers=None): ...

    @mcp.tool()
    @write_operation(requires_approval=True)
    async def publish_campaign(channel, ...): ...

Behavior:
- `@readonly` tools run without auth checks. They cannot mutate external
  state (no DB writes, no API calls that change anything).
- `@write_operation` tools require an `X-AdForge-Approval-Token` header
  that is HMAC-signed by the HITL review step. Without a valid token, the
  tool returns `{"error": "approval_required"}` instead of performing the
  write. Tokens are single-use + scoped to one campaign + one operation.

The LLM is never given the schemas of `@write_operation` tools — only the
orchestrator's publish step can call them, and only after HITL approval.
This is the architectural enforcement of "the LLM should not have edit
access".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from functools import wraps
from typing import Any, Callable

from adforge.config import get_settings


def _verify_approval_token(token: str, campaign_id: str, operation: str) -> bool:
    """Verify an approval token's HMAC signature + scope + single-use marker.

    Token format: ``<campaign_id>:<operation>:<expires_at>:<hmac>#<reviewer_email>``

    The HMAC is computed over ``campaign_id:operation:expires_at`` with
    ``APP_SECRET`` as the key. The reviewer_email is appended after a ``#``
    separator (informational — used for audit logging, not signature).

    Single-use enforcement is via a Redis SET — once consumed, the token
    is blacklisted (TODO v0.3 — currently tokens are valid until expiry).
    """
    s = get_settings()
    if not s.app_secret:
        # If no secret configured, no approval can be valid.
        return False

    try:
        # Strip the reviewer_email suffix (informational, not signed)
        signed_part = token.split("#", 1)[0]
        parts = signed_part.split(":")
        if len(parts) != 4:
            return False
        tok_campaign, tok_op, expires_str, signature = parts
        expires_at = int(expires_str)
        if time.time() > expires_at:
            return False
        if tok_campaign != campaign_id or tok_op != operation:
            return False

        # Verify HMAC
        message = f"{tok_campaign}:{tok_op}:{expires_str}".encode()
        expected = hmac.new(s.app_secret.encode(), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False

        return True
    except Exception:
        return False


def readonly(func: Callable) -> Callable:
    """Decorator marking a tool as read-only. No auth checks; no writes allowed.

    All read tools (web_search, query_kpis, compute_roas, evaluate_creative)
    use this. The function body must NOT perform any state mutation.
    """
    func.__adforge_permission__ = "readonly"  # type: ignore[attr-defined]
    return func


def write_operation(*, requires_approval: bool = True) -> Callable:
    """Decorator marking a tool as a write operation. Requires approval token.

    The decorated function receives the parsed approval context via
    ``_approval`` kwarg (campaign_id, operation, reviewer_email).

    If the request lacks a valid approval token, the tool returns
    ``{"error": "approval_required", "operation": <name>}`` instead of
    executing.
    """
    def decorator(func: Callable) -> Callable:
        func.__adforge_permission__ = "write"  # type: ignore[attr-defined]
        func.__adforge_requires_approval__ = requires_approval  # type: ignore[attr-defined]

        @wraps(func)
        async def wrapper(*args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> str:
            if not headers:
                return json.dumps({"error": "missing tenant header"})
            token = headers.get("X-AdForge-Approval-Token", "")
            campaign_id = headers.get("X-AdForge-Campaign", "")
            if not _verify_approval_token(token, campaign_id, func.__name__):
                return json.dumps({
                    "error": "approval_required",
                    "operation": func.__name__,
                    "campaign_id": campaign_id,
                    "hint": "HITL review must be approved before this write can execute",
                })
            # Token valid — invoke the underlying write.
            return await func(*args, headers=headers, **kwargs)

        return wrapper

    return decorator


def issue_approval_token(campaign_id: str, operation: str, reviewer_email: str, ttl_seconds: int = 3600) -> str:
    """Mint a single-use approval token. Called by the HITL review endpoint.

    The token allows ONE call to ``operation`` on ``campaign_id`` within
    ``ttl_seconds``. After use it must be blacklisted in Redis (TODO v0.3).
    """
    s = get_settings()
    if not s.app_secret:
        raise RuntimeError("APP_SECRET must be set to issue approval tokens")

    expires_at = int(time.time()) + ttl_seconds
    message = f"{campaign_id}:{operation}:{expires_at}".encode()
    signature = hmac.new(s.app_secret.encode(), message, hashlib.sha256).hexdigest()
    # Embed reviewer in the token metadata (not signed, but logged on use)
    return f"{campaign_id}:{operation}:{expires_at}:{signature}#{reviewer_email}"
