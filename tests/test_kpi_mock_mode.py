"""Unit tests for the dev/test-only deterministic mock KPI path.

The mock path is gated by ``APP_ENV in {dev, test}`` plus per-channel
``GOOGLE_ADS_USE_MOCK`` / ``META_USE_MOCK`` / ``LINKEDIN_USE_MOCK`` /
``TIKTOK_USE_MOCK`` settings. Staging/prod and ``use_mock=False`` must keep
the explicit missing-external-id error. No HTTP is involved — respx is not
needed here.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from support import ScriptedLLM

import adforge.agents.kpi_analytics.agent as kpi_agent_module
from adforge.agents.kpi_analytics.agent import KpiAnalyticsAgent
from adforge.agents.kpi_analytics.fetcher import (
    AdPlatformError,
    _fetch_mock_snapshots,
    fetch_channel_snapshots,
)
from adforge.config import get_settings
from adforge.guardrails.contracts import CampaignBrief
from adforge.guardrails.data_isolation import TenantScope

START = date(2026, 1, 1)
END = date(2026, 1, 10)
CAMPAIGN_ID = uuid4()


def _scope() -> TenantScope:
    return TenantScope(tenant_id="acme", campaign_id=CAMPAIGN_ID)


def _env(monkeypatch, **values: str) -> None:
    """Set environment variables and refresh the cached Settings instance."""
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def _brief(**overrides):
    data = {
        "product_name": "Acme",
        "product_description": "A project management tool for dev teams.",
        "target_audience": "Engineering managers",
        "budget_usd": 1000.0,
        "duration_days": 10,
        "channels_hint": ["google"],
    }
    data.update(overrides)
    return CampaignBrief(**data)


def _input(*, media_plan=None, **overrides):
    raw = {
        "request_id": str(uuid4()),
        "tenant_id": "tenant_a",
        "campaign_id": str(uuid4()),
        "agent_name": "kpi_analytics",
        "upstream_payloads": {},
        "brief": _brief().model_dump(),
        **overrides,
    }
    if media_plan is not None:
        raw["upstream_payloads"]["media_plan"] = media_plan
    return raw


class TestMockMode:
    async def test_dev_mock_returns_snapshots_without_external_id(self, monkeypatch):
        _env(monkeypatch, APP_ENV="dev", GOOGLE_ADS_USE_MOCK="true")
        first = await fetch_channel_snapshots(_scope(), "google", CAMPAIGN_ID, start=START, end=END)
        second = await fetch_channel_snapshots(
            _scope(), "google", CAMPAIGN_ID, start=START, end=END
        )
        assert first
        assert all(s.is_mock for s in first)
        assert all(s.source.startswith("mock_") for s in first)
        assert all(s.campaign_external_id == f"mock-google-{str(CAMPAIGN_ID)[:8]}" for s in first)
        # Deterministic: same inputs → identical snapshots across calls.
        assert first == second

    async def test_mock_short_circuits_even_with_external_id(self, monkeypatch):
        """Mock mode means no HTTP at all, even when an external id is given."""
        _env(monkeypatch, APP_ENV="dev", META_USE_MOCK="true")
        snapshots = await fetch_channel_snapshots(
            _scope(),
            "meta",
            CAMPAIGN_ID,
            external_id="real-123",
            start=START,
            end=END,
        )
        assert snapshots
        assert all(s.is_mock for s in snapshots)
        assert all(s.source == "mock_meta" for s in snapshots)

    async def test_staging_never_uses_mock(self, monkeypatch):
        _env(
            monkeypatch,
            APP_ENV="staging",
            GOOGLE_ADS_USE_MOCK="true",
            APP_REVIEWER_ALLOWLIST="ops@acme.test",
            API_AUTH_TOKEN="staging-token",
            CHECKPOINTER_BACKEND="postgres",
        )
        with pytest.raises(AdPlatformError, match="campaign_external_id"):
            await fetch_channel_snapshots(_scope(), "google", CAMPAIGN_ID, start=START, end=END)

    async def test_dev_with_mock_disabled_keeps_explicit_error(self, monkeypatch):
        _env(monkeypatch, APP_ENV="dev", LINKEDIN_USE_MOCK="false")
        with pytest.raises(AdPlatformError, match="campaign_external_id"):
            await fetch_channel_snapshots(_scope(), "linkedin", CAMPAIGN_ID, start=START, end=END)

    async def test_mock_respects_channel_budget(self, monkeypatch):
        _env(monkeypatch, APP_ENV="dev", TIKTOK_USE_MOCK="true")
        budget = 100.0
        snapshots = await fetch_channel_snapshots(
            _scope(),
            "tiktok",
            CAMPAIGN_ID,
            start=START,
            end=END,
            budget_usd=budget,
        )
        total_spend = sum(s.spend_usd for s in snapshots)
        assert total_spend > 0
        assert total_spend <= budget

    def test_mock_helper_is_stable_per_day(self):
        """Per-day seeding depends only on tenant/campaign/channel/date."""
        scope = _scope()
        rows = _fetch_mock_snapshots(scope, "google", CAMPAIGN_ID, start=START, end=END)
        assert len(rows) == (END - START).days + 1
        assert [r.platform_date for r in rows] == [date(2026, 1, day) for day in range(1, 11)]


class TestAgentMockPayload:
    async def test_agent_marks_payload_mock_and_reports_no_errors(self, monkeypatch):
        _env(monkeypatch, APP_ENV="dev", GOOGLE_ADS_USE_MOCK="true")

        async def fake_persist(scope, campaign_id, models):
            return len(models)

        # Persistence is disabled in unit tests; the audit trail is exercised
        # through BaseAgent._safe_audit, which tolerates the test fakes.
        monkeypatch.setattr(kpi_agent_module, "persist_snapshots", fake_persist)
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(
            _input(media_plan={"channel_allocations": [{"channel": "google", "budget_usd": 400.0}]})
        )
        assert result.ok is True
        payload = result.output.payload
        assert payload["is_mock"] is True
        assert payload["errors"] == []
        assert payload["snapshots"]
        assert all(row["is_mock"] for row in payload["snapshots"])
        assert all(row["source"].startswith("mock_") for row in payload["snapshots"])
        assert sum(row["spend_usd"] for row in payload["snapshots"]) <= 400.0
