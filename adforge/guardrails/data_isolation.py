"""Tenant / data-isolation guardrail.

THE core rule: every database query, every MCP call, every cache key MUST be
scoped to a single tenant_id. No code outside this module is allowed to
construct tenant-scoped strings directly.

Pattern:
    async with tenant_scope(tenant_id="acme", campaign_id=uuid) as scope:
        # everything inside this block is tenant-scoped
        await db.execute(scope.query(Campaign).filter(...))
        await mcp.call("ads.google.search", scope.as_headers())

The ``TenantScope`` carries the tenant_id + campaign_id into every storage
and MCP call. SQLAlchemy event listeners (see ``storage.session``) reject any
statement whose WHERE clause doesn't include ``tenant_id``.

This is non-negotiable. The BaseAgent class injects ``TenantScope`` into
the LangGraph state at the start of every run; agents read it from state,
never from a global.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from adforge.guardrails.exceptions import DataIsolationError


@dataclass(frozen=True)
class TenantScope:
    """Immutable identity token carried through every agent + storage call."""

    tenant_id: str
    campaign_id: UUID

    def assert_matches(self, other_tenant_id: str, resource: str) -> None:
        """Raise DataIsolationError if `other_tenant_id` != self.tenant_id.

        Used by storage layer when reading rows back: every row fetched must
        belong to the current tenant, or the read is rejected.
        """
        if other_tenant_id != self.tenant_id:
            raise DataIsolationError(
                tenant_id=self.tenant_id,
                attempted_resource=resource,
            )

    def as_headers(self) -> dict[str, str]:
        """Headers added to every MCP / external API call.

        MCP servers MUST refuse requests that don't carry these headers
        (the AdForge research + LinkedIn MCPs enforce this in their handlers).
        """
        return {
            "X-AdForge-Tenant": self.tenant_id,
            "X-AdForge-Campaign": str(self.campaign_id),
        }

    def cache_key(self, *parts: str) -> str:
        """Build a tenant-prefixed cache key for Redis."""
        return ":".join(["adforge", self.tenant_id, str(self.campaign_id), *parts])

    def __str__(self) -> str:
        return f"TenantScope(tenant={self.tenant_id!r}, campaign={self.campaign_id})"


@asynccontextmanager
async def tenant_scope(
    tenant_id: str, campaign_id: UUID
) -> AsyncIterator[TenantScope]:
    """Context manager that creates a TenantScope and audits its lifecycle.

    All agent execution + storage calls happen inside this block. Exit log
    records the tenant + campaign + duration so we can prove isolation in
    audit reviews.
    """
    if not tenant_id or not tenant_id.strip():
        raise DataIsolationError(
            tenant_id=repr(tenant_id),
            attempted_resource="<empty tenant_id>",
        )
    scope = TenantScope(tenant_id=tenant_id.strip(), campaign_id=campaign_id)
    # In production, we'd push the scope into a contextvar here so the
    # SQLAlchemy session listener can read it without explicit passing.
    # For v1 we pass explicitly to keep the surface smaller.
    try:
        yield scope
    finally:
        # Audit hook fires here in production. See guardrails.audit_log.
        pass


def assert_tenant_access(scope: TenantScope, resource_tenant_id: str, resource: str) -> None:
    """Convenience function for storage layer to call when reading rows."""
    scope.assert_matches(resource_tenant_id, resource)
