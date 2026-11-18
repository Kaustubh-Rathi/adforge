"""Integration tests for the tenant-isolation guardrail.

These exercise ``tenant_scope`` against real rows in PostgreSQL: a scope may
read and validate its own tenant's rows, and must reject any row that belongs
to a different tenant. That is the last line of defence if a query forgets its
``WHERE tenant_id`` clause.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from adforge.guardrails.data_isolation import (
    TenantScope,
    assert_tenant_access,
    tenant_scope,
)
from adforge.guardrails.exceptions import DataIsolationError
from adforge.storage import session as session_module

pytestmark = pytest.mark.integration


async def _insert_campaign(tenant_id: str, name: str) -> str:
    from sqlalchemy import text

    campaign_id = uuid4()
    factory = session_module.get_session_factory()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO campaigns (id, tenant_id, name, brief, status) "
                "VALUES (:id, :tid, :name, CAST(:brief AS JSONB), 'draft')"
            ),
            {
                "id": str(campaign_id),
                "tid": tenant_id,
                "name": name,
                "brief": json.dumps({"product_name": name}),
            },
        )
        await session.commit()
    return str(campaign_id)


async def _tenant_names(session, tenant_id: str) -> list[str]:
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT name FROM campaigns WHERE tenant_id = :tid ORDER BY name"),
        {"tid": tenant_id},
    )
    return [row[0] for row in result]


class TestTenantScopeAgainstDatabase:
    async def test_scope_reads_only_its_own_tenants_rows(self, db_env):
        await _insert_campaign("acme", "Acme Launch")
        await _insert_campaign("other", "Other Launch")

        async with tenant_scope("  acme  ", uuid4()) as scope:
            assert scope.tenant_id == "acme"  # whitespace is normalised
            factory = session_module.get_session_factory()
            async with factory() as session:
                names = await _tenant_names(session, scope.tenant_id)

            assert names == ["Acme Launch"]
            scope.assert_matches("acme", "campaign")  # own row is allowed
            with pytest.raises(DataIsolationError, match="data-isolation"):
                scope.assert_matches("other", "campaign")
            with pytest.raises(DataIsolationError):
                assert_tenant_access(scope, "other", "campaign")

    async def test_scope_headers_and_cache_keys_are_tenant_prefixed(self, db_env):
        campaign_id = uuid4()
        async with tenant_scope("acme", campaign_id) as scope:
            assert scope.as_headers() == {
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Campaign": str(campaign_id),
            }
            key = scope.cache_key("kpi", "google")
            assert key == f"adforge:acme:{campaign_id}:kpi:google"
            assert "acme" in str(scope)
            assert str(campaign_id) in str(scope)

    async def test_blank_tenant_is_rejected_before_any_db_access(self, db_env):
        with pytest.raises(DataIsolationError, match="empty tenant_id"):
            async with tenant_scope("   ", uuid4()):
                raise AssertionError("body must not run for a blank tenant")

    async def test_scope_is_immutable(self, db_env):
        from dataclasses import FrozenInstanceError

        scope = TenantScope(tenant_id="acme", campaign_id=uuid4())
        with pytest.raises(FrozenInstanceError):
            scope.tenant_id = "intruder"  # frozen dataclass
        assert scope.tenant_id == "acme"
