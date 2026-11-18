"""Integration tests for the storage layer (embedded PostgreSQL).

Covers engine lifecycle, transaction semantics, and the developer seed script
against the real schema rather than mocks.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

import adforge.storage.seed as seed_module
import adforge.storage.session as session_module
from adforge.mcp_servers import analytics

pytestmark = pytest.mark.integration


class TestEngineLifecycle:
    async def test_init_engine_is_idempotent(self, db_env):
        first = session_module._engine
        session_module.init_engine()
        assert session_module._engine is first
        assert session_module.get_session_factory() is session_module._session_factory

    async def test_session_scope_commits(self, db_env):
        campaign_id = uuid4()
        async with session_module.session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO campaigns (id, tenant_id, name, brief, status) "
                    "VALUES (:id, 'scope', 'Committed', CAST(:brief AS JSONB), 'draft')"
                ),
                {"id": str(campaign_id), "brief": json.dumps({"product_name": "x"})},
            )

        async with session_module.session_scope() as session:
            found = await session.execute(
                text("SELECT name FROM campaigns WHERE id = :id"),
                {"id": str(campaign_id)},
            )
            assert found.scalar_one() == "Committed"

    async def test_session_scope_rolls_back(self, db_env):
        campaign_id = uuid4()
        with pytest.raises(RuntimeError, match="boom"):
            async with session_module.session_scope() as session:
                await session.execute(
                    text(
                        "INSERT INTO campaigns (id, tenant_id, name, brief, status) "
                        "VALUES (:id, 'scope', 'Rolled back', CAST(:brief AS JSONB), 'draft')"
                    ),
                    {"id": str(campaign_id), "brief": json.dumps({"product_name": "x"})},
                )
                raise RuntimeError("boom")

        async with session_module.session_scope() as session:
            found = await session.execute(
                text("SELECT COUNT(*) FROM campaigns WHERE id = :id"),
                {"id": str(campaign_id)},
            )
            assert found.scalar_one() == 0

    async def test_lifespan_initialises_and_disposes(self, db_env):
        from adforge.api.main import app

        async with app.router.lifespan_context(app):
            assert session_module._engine is not None
        assert session_module._engine is None


class TestSeed:
    async def test_seed_inserts_campaigns_kpis_and_rolls_up_totals(
        self, db_env, tmp_path, monkeypatch, capsys
    ):
        examples = [
            {
                "tenant_id": "seed_tenant",
                "name": "Seed Campaign",
                "days_live": 2,
                "avg_conversion_value": 100.0,
                "brief": {
                    "product_name": "Seed Product",
                    "product_description": "A seeded product.",
                    "target_audience": "Developers",
                    "budget_usd": 5000.0,
                    "duration_days": 2,
                    "channels_hint": ["google", "linkedin"],
                },
            }
        ]
        examples_file = tmp_path / "campaigns.json"
        examples_file.write_text(json.dumps(examples), encoding="utf-8")
        monkeypatch.setattr(seed_module, "EXAMPLES_FILE", examples_file)

        await seed_module.seed()
        assert "Seeded 1 example campaigns" in capsys.readouterr().out

        # seed() disposes the engine it used; bring it back for assertions.
        session_module.init_engine()
        factory = session_module.get_session_factory()
        async with factory() as session:
            campaign = (
                await session.execute(
                    text(
                        "SELECT id, total_spend_usd, total_revenue_usd FROM campaigns "
                        "WHERE tenant_id = 'seed_tenant'"
                    )
                )
            ).mappings().one()
            kpi_count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM kpi_snapshots WHERE tenant_id = 'seed_tenant'"
                    )
                )
            ).scalar_one()

        assert kpi_count == 4  # 2 channels x 2 days
        assert float(campaign["total_spend_usd"]) > 0
        assert float(campaign["total_revenue_usd"]) > 0

        # Seeded rows carry platform_date, so the dashboard query sees them.
        today = datetime.now(UTC).date()
        rows = json.loads(
            await analytics.query_kpis(
                str(campaign["id"]),
                days_back=7,
                headers={"X-AdForge-Tenant": "seed_tenant"},
            )
        )
        assert rows["count"] == 4
        assert {row["platform_date"] for row in rows["snapshots"]} == {
            today.isoformat(),
            (today - timedelta(days=1)).isoformat(),
        }
        assert all(row["source"] == "seed" for row in rows["snapshots"])

    async def test_reseeding_is_idempotent(
        self, db_env, tmp_path, monkeypatch, capsys
    ):
        """`make seed` twice must not clone the demo campaigns and their KPIs."""
        examples = [
            {
                "tenant_id": "seed_tenant",
                "name": "Seed Campaign",
                "days_live": 2,
                "avg_conversion_value": 100.0,
                "brief": {
                    "product_name": "Seed Product",
                    "product_description": "A seeded product.",
                    "target_audience": "Developers",
                    "budget_usd": 5000.0,
                    "duration_days": 2,
                    "channels_hint": ["google", "linkedin"],
                },
            }
        ]
        examples_file = tmp_path / "campaigns.json"
        examples_file.write_text(json.dumps(examples), encoding="utf-8")
        monkeypatch.setattr(seed_module, "EXAMPLES_FILE", examples_file)

        await seed_module.seed()
        await seed_module.seed()
        capsys.readouterr()

        session_module.init_engine()
        factory = session_module.get_session_factory()
        async with factory() as session:
            campaigns = (
                await session.execute(
                    text("SELECT COUNT(*) FROM campaigns WHERE tenant_id = 'seed_tenant'")
                )
            ).scalar_one()
            kpi_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM kpi_snapshots WHERE tenant_id = 'seed_tenant'")
                )
            ).scalar_one()

        assert campaigns == 1
        assert kpi_count == 4  # not 8: the second run replaces, not appends

    def test_seed_campaign_ids_are_stable(self):
        first = seed_module._campaign_id("acme", "Launch")
        assert first == seed_module._campaign_id("acme", "Launch")
        assert first != seed_module._campaign_id("acme", "Other")
        assert first != seed_module._campaign_id("other", "Launch")
