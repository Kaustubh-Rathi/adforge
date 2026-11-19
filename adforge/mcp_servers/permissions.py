"""MCP tool permissions — read-only vs write-operation decorators.

Every MCP tool MUST be decorated with either ``@readonly`` or
``@write_operation``. The ``mcp.tool()`` decorator from FastMCP goes on top of
these.

Behavior:
- ``@readonly`` tools run without approval. They must not mutate external state.
- ``@write_operation`` tools require an ``X-AdForge-Approval-Token`` header
  that is HMAC-signed by the HITL review step. Tokens are bound to a tenant,
  campaign, operation, reviewer, expiry, and a unique id, and are consumed
  atomically in Redis (single use).

The LLM is never given the schemas of write tools. Only the orchestrator's
post-approval publish step can call them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from mcp.types import ToolAnnotations

from adforge.config import get_settings

#: MCP tool annotations. The LLM tool loop only exposes tools marked
#: ``readOnlyHint=True``; unannotated tools fail closed.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(secret: str, payload_b64: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def _verify_approval_claims(
    token: str,
    campaign_id: str,
    operation: str,
    *,
    tenant_id: str = "",
) -> dict[str, Any] | None:
    """Verify signature + scope and return the signed claims, or ``None``.

    ``tenant_id`` is optional for backwards-compatible direct unit tests; the
    write decorator always supplies it, so production writes are tenant-bound.
    """
    s = get_settings()
    if not s.app_secret or not token:
        return None

    try:
        prefix, payload_b64, signature = token.split(".", 2)
        if prefix != "v2":
            return None
        expected = _sign(s.app_secret, payload_b64)
        if not hmac.compare_digest(expected, signature):
            return None

        claims = json.loads(_b64decode(payload_b64))
        if not isinstance(claims, dict):
            return None
        if int(claims.get("expires_at", 0)) < time.time():
            return None
        if str(claims.get("campaign_id")) != str(campaign_id):
            return None
        if claims.get("operation") != operation:
            return None
        if tenant_id and claims.get("tenant_id") != tenant_id:
            return None
        if not claims.get("jti"):
            return None
        return claims
    except Exception:
        return None


def _verify_approval_token(
    token: str, campaign_id: str, operation: str, *, tenant_id: str = ""
) -> bool:
    """Backwards-compatible boolean verification helper."""
    return _verify_approval_claims(token, campaign_id, operation, tenant_id=tenant_id) is not None


async def _consume_approval_token(claims: dict[str, Any]) -> bool:
    """Atomically mark a token as used. Fails closed on Redis errors."""
    from adforge.storage.redis_client import get_redis

    ttl = max(1, int(claims["expires_at"]) - int(time.time()))
    key = f"adforge:approval:used:{claims['jti']}"
    try:
        redis = await get_redis()
        stored = await redis.set(key, "1", nx=True, ex=ttl)
        return bool(stored)
    except Exception:
        return False


async def verify_and_consume_approval(
    token: str,
    campaign_id: str,
    operation: str,
    *,
    tenant_id: str = "",
    reviewer_email: str | None = None,
) -> dict[str, Any] | None:
    """Verify scope/reviewer, then atomically consume a single-use token.

    Returns the signed claims on success and ``None`` for any failure
    (invalid signature, wrong tenant/campaign/operation, wrong reviewer,
    expired, already used, or an unavailable token store).
    """
    claims = _verify_approval_claims(token, campaign_id, operation, tenant_id=tenant_id)
    if claims is None:
        return None
    if reviewer_email is not None:
        bound = str(claims.get("reviewer_email") or "")
        if bound.lower() != reviewer_email.lower():
            return None
    if not await _consume_approval_token(claims):
        return None
    return claims


def readonly(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator marking a tool as read-only. No writes allowed."""
    func.__adforge_permission__ = "readonly"  # type: ignore[attr-defined]
    return func


def write_operation(*, requires_approval: bool = True) -> Callable[..., Any]:
    """Decorator marking a tool as a write operation.

    If ``requires_approval`` is true, the request must carry a valid,
    tenant-bound, single-use approval token.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__adforge_permission__ = "write"  # type: ignore[attr-defined]
        func.__adforge_requires_approval__ = requires_approval  # type: ignore[attr-defined]

        @wraps(func)
        async def wrapper(*args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> str:
            if not headers or "X-AdForge-Tenant" not in headers:
                return json.dumps({"error": "missing tenant header"})
            if requires_approval:
                token = headers.get("X-AdForge-Approval-Token", "")
                campaign_id = headers.get("X-AdForge-Campaign", "")
                tenant_id = headers["X-AdForge-Tenant"]
                claims = _verify_approval_claims(
                    token, campaign_id, func.__name__, tenant_id=tenant_id
                )
                if claims is None:
                    return json.dumps(
                        {
                            "error": "approval_required",
                            "operation": func.__name__,
                            "campaign_id": campaign_id,
                            "hint": ("HITL review must be approved before this write can execute"),
                        }
                    )
                if not await _consume_approval_token(claims):
                    return json.dumps(
                        {
                            "error": "approval_token_already_used_or_store_unavailable",
                            "operation": func.__name__,
                            "campaign_id": campaign_id,
                        }
                    )
            result: Any = await func(*args, headers=headers, **kwargs)
            return str(result)

        return wrapper

    return decorator


def issue_approval_token(
    campaign_id: str,
    operation: str,
    reviewer_email: str,
    ttl_seconds: int = 3600,
    *,
    tenant_id: str = "",
) -> str:
    """Mint a single-use, tenant-bound approval token.

    The reviewer email is part of the signed payload, so the token cannot be
    altered to impersonate another reviewer.
    """
    s = get_settings()
    if not s.app_secret:
        raise RuntimeError("APP_SECRET must be set to issue approval tokens")

    now = int(time.time())
    claims = {
        "tenant_id": tenant_id,
        "campaign_id": str(campaign_id),
        "operation": operation,
        "reviewer_email": reviewer_email,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
        "jti": secrets.token_urlsafe(18),
    }
    payload_b64 = _b64encode(json.dumps(claims, separators=(",", ":")).encode())
    signature = _sign(s.app_secret, payload_b64)
    return f"v2.{payload_b64}.{signature}"
