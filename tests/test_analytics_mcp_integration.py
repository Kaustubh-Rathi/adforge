"""Integration tests for the analytics MCP server + KPI persistence.

Uses the embedded PostgreSQL server from ``tests/conftest.py``. These cover the
contract the dashboard and KPI agent depend on: idempotent daily upserts,
tenant scoping, channel/day aggregation, and campaign total roll-ups.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from adforge.agents.kpi_analytics.fetcher import persist_snapshots
from adforge.guardrails.contracts import KpiSnapshot
from adforge.guardrails.data_isolation import TenantScope
from adforge.mcp_servers import analytics

pytestmark = pytest.mark.integration

TODAY = datetime.now(UTC).date()
DAY_ONE = TODAY - timedelta(days=1)
DAY_TWO = TODAY


async def _create_campaign(db_env, tenant_id: str, name: str = "KPI Test") -> UUID:
    from sqlalchemy import text

    from adforge.storage.session import get_session_factory

    campaign_id = uuid4()
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO campaigns (id, tenant_id, name, brief, status) "
                "VALUES (:id, :tid, :name, CAST(:brief AS JSONB), 'live')"
            ),
            {
                "id": str(campaign_id),
                "tid": tenant_id,
                "name": name,
                "brief": json.dumps({"product_name": name}),
            },
        )
        await session.commit()
    return campaign_id


def _snapshot(
    campaign_id: UUID,
    *,
    channel: str = "google",
    day: date = DAY_ONE,
    impressions: int = 1000,
    clicks: int = 100,
    spend: float = 50.0,
    conversions: int = 10,
    revenue: float = 200.0,
    source: str = "google_ads",
) -> KpiSnapshot:
    return KpiSnapshot(
        channel=channel,
        campaign_external_id="ext-1",
        impressions=impressions,
        clicks=clicks,
        spend_usd=spend,
        conversions=conversions,
        revenue_usd=revenue,
        ctr=round(clicks / impressions, 4) if impressions else 0.0,
        cvr=round(conversions / clicks, 4) if clicks else 0.0,
        cpc=round(spend / clicks, 2) if clicks else 0.0,
        cpa=round(spend / conversions, 2) if conversions else 0.0,
        cpm=round(spend / impressions * 1000, 2) if impressions else 0.0,
        roas=round(revenue / spend, 2) if spend else 0.0,
        captured_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        platform_date=day,
        source=source,
        is_mock=False,
    )


def _payload(snap: KpiSnapshot, campaign_id: UUID) -> str:
    data = snap.model_dump(mode="json")
    data["campaign_id"] = str(campaign_id)
    return json.dumps(data)


class TestAnalyticsMcp:
    async def test_persist_query_and_aggregate(self, db_env):
        tenant = "acme"
        campaign_id = await _create_campaign(db_env, tenant)
        headers = {"X-AdForge-Tenant": tenant}

        first = _snapshot(campaign_id)
        second = _snapshot(
            campaign_id,
            channel="linkedin",
            impressions=500,
            clicks=25,
            spend=20.0,
            conversions=5,
            revenue=100.0,
            source="linkedin_ads",
        )
        for snap in (first, second):
            result = json.loads(
                await analytics.persist_kpi_snapshot(
                    _payload(snap, campaign_id), headers=headers
                )
            )
            assert result["status"] == "persisted"
            UUID(result["id"])  # returned id is a UUID

        rows = json.loads(
            await analytics.query_kpis(str(campaign_id), headers=headers)
        )
        assert rows["count"] == 2
        channels = {row["channel"] for row in rows["snapshots"]}
        assert channels == {"google", "linkedin"}
        assert all(row["tenant_id"] == tenant for row in rows["snapshots"])

        filtered = json.loads(
            await analytics.query_kpis(
                str(campaign_id), channel="google", headers=headers
            )
        )
        assert filtered["count"] == 1
        assert filtered["snapshots"][0]["channel"] == "google"

        by_channel = json.loads(
            await analytics.aggregate_kpis(
                str(campaign_id), group_by="channel", headers=headers
            )
        )
        assert {b["bucket"] for b in by_channel["buckets"]} == {"google", "linkedin"}
        # Provenance travels with the aggregate: real rows must not be flagged.
        assert all(b["is_mock"] is False for b in by_channel["buckets"])

        by_day = json.loads(
            await analytics.aggregate_kpis(
                str(campaign_id), group_by="day", headers=headers
            )
        )
        assert len(by_day["buckets"]) == 1  # both snapshots are on 2026-01-05

    async def test_same_day_upsert_updates_instead_of_duplicating(self, db_env):
        tenant = "acme"
        campaign_id = await _create_campaign(db_env, tenant)
        headers = {"X-AdForge-Tenant": tenant}

        original = _snapshot(campaign_id, impressions=1000, clicks=100, spend=50.0)
        updated = _snapshot(campaign_id, impressions=2000, clicks=200, spend=80.0)

        await analytics.persist_kpi_snapshot(_payload(original, campaign_id), headers=headers)
        await analytics.persist_kpi_snapshot(_payload(updated, campaign_id), headers=headers)

        rows = json.loads(
            await analytics.query_kpis(str(campaign_id), headers=headers)
        )
        assert rows["count"] == 1
        assert rows["snapshots"][0]["impressions"] == 2000
        assert rows["snapshots"][0]["spend_usd"] == 80.0

    async def test_tenant_isolation_on_queries(self, db_env):
        campaign_id = await _create_campaign(db_env, "acme")
        await analytics.persist_kpi_snapshot(
            _payload(_snapshot(campaign_id), campaign_id),
            headers={"X-AdForge-Tenant": "acme"},
        )

        other = json.loads(
            await analytics.query_kpis(
                str(campaign_id), headers={"X-AdForge-Tenant": "intruder"}
            )
        )
        assert other == {"snapshots": [], "count": 0}

    async def test_missing_tenant_header_is_rejected(self, db_env):
        campaign_id = await _create_campaign(db_env, "acme")
        persisted = json.loads(
            await analytics.persist_kpi_snapshot(_payload(_snapshot(campaign_id), campaign_id))
        )
        assert persisted["error"] == "missing tenant header"

        queried = json.loads(await analytics.query_kpis(str(campaign_id)))
        assert queried["error"] == "missing tenant header"
        aggregated = json.loads(await analytics.aggregate_kpis(str(campaign_id)))
        assert aggregated["error"] == "missing tenant header"

    async def test_invalid_group_by_is_rejected(self, db_env):
        result = json.loads(
            await analytics.aggregate_kpis(
                str(uuid4()), group_by="week", headers={"X-AdForge-Tenant": "acme"}
            )
        )
        assert "group_by" in result["error"]


class TestSnapshotCoercion:
    """JSON callers send strings; asyncpg requires real date/datetime values."""

    def test_coerce_date_accepts_supported_shapes(self):
        assert analytics._coerce_date(
            datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ) == date(2026, 1, 5)
        assert analytics._coerce_date(date(2026, 1, 5)) == date(2026, 1, 5)
        assert analytics._coerce_date("2026-01-05T12:00:00Z") == date(2026, 1, 5)
        # Compact YYYYMMDDThhmmss is accepted because the date prefix is
        # parsed after slicing off the time component.
        assert analytics._coerce_date("20260105T120000") == date(2026, 1, 5)

    def test_coerce_datetime_accepts_supported_shapes(self):
        aware = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        assert analytics._coerce_datetime(aware) is aware
        assert analytics._coerce_datetime("2026-01-05T12:00:00Z") == aware
        assert analytics._coerce_datetime("20260105120000") == datetime(
            2026, 1, 5, tzinfo=UTC
        )

        naive = analytics._coerce_datetime("2026-01-05T12:00:00")
        assert naive == aware
        assert naive.tzinfo is UTC

        # Date-only strings and date strings with a trailing Z exercise the
        # ValueError fallback that reuses _coerce_date.
        assert analytics._coerce_datetime("2026-01-05") == datetime(
            2026, 1, 5, tzinfo=UTC
        )
        assert analytics._coerce_datetime("2026-01-05Z") == datetime(
            2026, 1, 5, tzinfo=UTC
        )

    async def test_persist_accepts_string_dates_from_json_callers(self, db_env):
        tenant = "acme"
        campaign_id = await _create_campaign(db_env, tenant)
        headers = {"X-AdForge-Tenant": tenant}

        data = json.loads(_payload(_snapshot(campaign_id), campaign_id))
        data["platform_date"] = DAY_ONE.isoformat()
        data["captured_at"] = f"{DAY_ONE.isoformat()}T09:30:00"  # naive on purpose

        persisted = json.loads(
            await analytics.persist_kpi_snapshot(json.dumps(data), headers=headers)
        )
        assert persisted["status"] == "persisted"

        rows = json.loads(
            await analytics.query_kpis(str(campaign_id), headers=headers)
        )
        assert rows["count"] == 1
        assert rows["snapshots"][0]["platform_date"] == DAY_ONE.isoformat()
        assert rows["snapshots"][0]["captured_at"].startswith(
            f"{DAY_ONE.isoformat()}T09:30:00"
        )


class TestKpiPersistenceRollup:
    async def test_idempotent_upsert_and_campaign_totals(self, db_env):
        tenant = "acme"
        campaign_id = await _create_campaign(db_env, tenant)
        scope = TenantScope(tenant_id=tenant, campaign_id=campaign_id)

        day_one = _snapshot(campaign_id, day=DAY_ONE, spend=50.0, revenue=200.0)
        day_two = _snapshot(campaign_id, day=DAY_TWO, spend=30.0, revenue=120.0)

        assert await persist_snapshots(scope, campaign_id, [day_one, day_two]) == 2
        # Re-running with updated values must update, not duplicate.
        updated = _snapshot(
            campaign_id, day=DAY_ONE, spend=60.0, revenue=240.0
        )
        assert await persist_snapshots(scope, campaign_id, [updated]) == 1

        from sqlalchemy import text

        from adforge.storage.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS n, SUM(spend_usd) AS spend, "
                        "SUM(revenue_usd) AS revenue FROM kpi_snapshots "
                        "WHERE tenant_id = :tid AND campaign_id = :cid"
                    ),
                    {"tid": tenant, "cid": str(campaign_id)},
                )
            ).mappings().one()
            campaign = (
                await session.execute(
                    text(
                        "SELECT total_spend_usd, total_revenue_usd FROM campaigns "
                        "WHERE id = :cid AND tenant_id = :tid"
                    ),
                    {"cid": str(campaign_id), "tid": tenant},
                )
            ).mappings().one()

        assert row["n"] == 2
        assert float(row["spend"]) == 90.0
        assert float(row["revenue"]) == 360.0
        assert float(campaign["total_spend_usd"]) == 90.0
        assert float(campaign["total_revenue_usd"]) == 360.0

    async def test_rollup_does_not_touch_other_tenants(self, db_env):
        campaign_id = await _create_campaign(db_env, "acme")
        other_id = await _create_campaign(db_env, "other")
        scope = TenantScope(tenant_id="acme", campaign_id=campaign_id)

        await persist_snapshots(scope, campaign_id, [_snapshot(campaign_id)])

        from sqlalchemy import text

        from adforge.storage.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            other = (
                await session.execute(
                    text(
                        "SELECT total_spend_usd FROM campaigns WHERE id = :cid"
                    ),
                    {"cid": str(other_id)},
                )
            ).scalar_one()
        assert float(other) == 0.0
