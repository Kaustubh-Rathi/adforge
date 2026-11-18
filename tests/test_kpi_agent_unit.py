"""Unit tests for the KPI/Analytics agent node (deterministic fetch + persist)."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

from support import ScriptedLLM

import adforge.agents.kpi_analytics.agent as kpi_agent_module
from adforge.agents.kpi_analytics.agent import KpiAnalyticsAgent
from adforge.guardrails.contracts import AgentInput, CampaignBrief


def _brief(**overrides):
    data = {
        "product_name": "Acme",
        "product_description": "A project management tool for dev teams.",
        "target_audience": "Engineering managers",
        "budget_usd": 1000.0,
        "duration_days": 7,
        "channels_hint": ["google", "linkedin"],
    }
    data.update(overrides)
    return CampaignBrief(**data)


def _input(*, media_plan=None, brief=True, **overrides):
    raw = {
        "request_id": str(uuid4()),
        "tenant_id": "tenant_a",
        "campaign_id": str(uuid4()),
        "agent_name": "kpi_analytics",
        "upstream_payloads": {},
        "brief": _brief().model_dump() if brief else None,
        **overrides,
    }
    if media_plan is not None:
        raw["upstream_payloads"]["media_plan"] = media_plan
    return raw


class TestKpiAgentBasics:
    def test_build_messages_and_parse_response(self):
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        messages = agent.build_messages(AgentInput(**_input()))
        assert messages[0]["role"] == "system"
        assert "KPI" in messages[0]["content"] or "KPI" in messages[1]["content"]
        assert agent.parse_response("{}", None) == {"snapshots": []}


class TestKpiFetch:
    @staticmethod
    def _fake_fetch(rows_per_call: int = 1):
        from adforge.agents.kpi_analytics.fetcher import build_snapshot

        async def _fetch(scope, channel, campaign_id, **kwargs):
            return [
                build_snapshot(
                    channel=channel,
                    campaign_id=campaign_id,
                    campaign_external_id=kwargs.get("external_id"),
                    platform_date=date(2026, 1, day),
                    impressions=100,
                    clicks=10,
                    spend_usd=5.0,
                    conversions=1,
                    revenue_usd=20.0,
                    source=f"{channel}_ads",
                )
                for day in range(1, rows_per_call + 1)
            ]

        return _fetch

    async def test_media_plan_allocations_drive_fetch(self, monkeypatch):
        monkeypatch.setattr(
            kpi_agent_module, "fetch_channel_snapshots", self._fake_fetch(7)
        )
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        plan = {
            "channel_allocations": [
                {
                    "channel": "google",
                    "budget_usd": 500.0,
                    "campaign_external_id": "g-1",
                },
                {
                    "channel": "linkedin",
                    "budget_usd": 500.0,
                    "campaign_external_id": "li-1",
                },
            ]
        }
        result = await agent.run(_input(media_plan=plan))
        assert result.ok is True
        payload = result.output.payload
        assert payload["channel_count"] == 2
        assert len(payload["snapshots"]) == 14
        assert payload["is_mock"] is False
        # Persistence is unavailable in unit tests, so it is recorded as a
        # non-fatal failure rather than losing the fetched data.
        assert any(error.startswith("persist:") for error in payload["errors"])

    async def test_brief_channels_are_used_when_media_plan_missing(self, monkeypatch):
        monkeypatch.setattr(
            kpi_agent_module, "fetch_channel_snapshots", self._fake_fetch(1)
        )
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(_input())
        assert result.ok is True
        assert result.output.payload["channel_count"] == 2

    async def test_no_allocations_returns_empty_payload(self):
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(_input(brief=False))
        assert result.ok is True
        payload = result.output.payload
        assert payload["snapshots"] == []
        assert payload["channel_count"] == 0
        assert payload["persisted"] == 0
        assert payload["is_mock"] is False

    async def test_all_channels_failing_is_an_agent_error(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("platform down")

        monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", boom)
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(
            _input(
                media_plan={
                    "channel_allocations": [
                        {"channel": "google", "budget_usd": 1.0},
                        {"channel": "linkedin", "budget_usd": 1.0},
                    ]
                }
            )
        )
        assert result.ok is False
        assert "kpi_fetch:google,linkedin" in result.output.error
        assert "platform down" in result.output.error

    async def test_partial_channel_failure_still_returns_data(self, monkeypatch):
        from adforge.agents.kpi_analytics.fetcher import build_snapshot

        async def flaky(scope, channel, campaign_id, **kwargs):
            if channel == "linkedin":
                raise RuntimeError("linkedin down")
            return [
                build_snapshot(
                    channel=channel,
                    campaign_id=campaign_id,
                    campaign_external_id=kwargs.get("external_id"),
                    platform_date=date(2026, 1, 1),
                    impressions=10,
                    clicks=1,
                    spend_usd=1.0,
                    conversions=0,
                    revenue_usd=0.0,
                    source=f"{channel}_ads",
                )
            ]

        monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", flaky)
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(
            _input(
                media_plan={
                    "channel_allocations": [
                        {"channel": "google", "budget_usd": 1.0},
                        {"channel": "linkedin", "budget_usd": 1.0},
                    ]
                }
            )
        )
        assert result.ok is True
        payload = result.output.payload
        assert len(payload["snapshots"]) == 1
        assert any("linkedin" in error for error in payload["errors"])

    async def test_persistence_success_is_reported(self, monkeypatch):
        async def fake_persist(scope, campaign_id, models):
            assert scope.tenant_id == "tenant_a"
            return len(models)

        monkeypatch.setattr(
            kpi_agent_module, "fetch_channel_snapshots", self._fake_fetch(7)
        )
        monkeypatch.setattr(kpi_agent_module, "persist_snapshots", fake_persist)
        agent = KpiAnalyticsAgent(llm=ScriptedLLM(default={}))
        result = await agent.run(
            _input(
                media_plan={
                    "channel_allocations": [
                        {"channel": "google", "budget_usd": 1.0}
                    ]
                }
            )
        )
        assert result.ok is True
        assert result.output.payload["persisted"] == 7
        assert result.output.payload["errors"] == []
