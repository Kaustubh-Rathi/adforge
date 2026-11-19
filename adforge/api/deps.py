"""Shared API dependencies."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.config import get_settings
from adforge.guardrails.data_isolation import TenantScope
from adforge.storage import session as session_module


def _require_api_key(x_api_key: str | None) -> None:
    """Validate the shared API key when one is configured.

    Dev/test deployments may leave ``API_AUTH_TOKEN`` blank; staging/prod
    refuse to start without it (see ``Settings._production_safety``).
    """
    expected = get_settings().api_auth_token
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


async def get_tenant(
    x_adforge_tenant: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Extract tenant_id from request headers. Required on every API call."""
    _require_api_key(x_api_key)
    if not x_adforge_tenant:
        raise HTTPException(status_code=400, detail="X-AdForge-Tenant header is required")
    return x_adforge_tenant


async def get_tenant_scope(
    x_adforge_tenant: str = Header(default=""),
    x_adforge_campaign: str = Header(default=""),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TenantScope:
    """Build a TenantScope from headers. Used for tenant-isolated DB queries."""
    _require_api_key(x_api_key)
    if not x_adforge_tenant:
        raise HTTPException(status_code=400, detail="X-AdForge-Tenant header is required")
    try:
        campaign_id = UUID(x_adforge_campaign) if x_adforge_campaign else UUID(int=0)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="X-AdForge-Campaign must be a valid UUID",
        ) from exc
    return TenantScope(tenant_id=x_adforge_tenant, campaign_id=campaign_id)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a per-request session."""
    factory = session_module.get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
