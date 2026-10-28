"""Shared API dependencies."""
from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.guardrails.data_isolation import TenantScope
from adforge.storage.session import get_session_factory


async def get_tenant(x_adforge_tenant: str | None = Header(default=None)) -> str:
    """Extract tenant_id from request headers. Required on every API call."""
    if not x_adforge_tenant:
        raise HTTPException(status_code=400, detail="X-AdForge-Tenant header is required")
    return x_adforge_tenant


async def get_tenant_scope(
    x_adforge_tenant: str = Header(default=""),
    x_adforge_campaign: str = Header(default=""),
) -> TenantScope:
    """Build a TenantScope from headers. Used for tenant-isolated DB queries."""
    if not x_adforge_tenant:
        raise HTTPException(status_code=400, detail="X-AdForge-Tenant header is required")
    try:
        campaign_id = UUID(x_adforge_campaign) if x_adforge_campaign else UUID(int=0)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-AdForge-Campaign must be a valid UUID")
    return TenantScope(tenant_id=x_adforge_tenant, campaign_id=campaign_id)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a per-request session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
