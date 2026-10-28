"""Tests for the guardrails layer — contracts, isolation, policy, rate limiter."""
from __future__ import annotations

from uuid import uuid4

import pytest

from adforge.guardrails.contracts import (
    CampaignBrief, AgentInput, AgentOutput, ResearchFindings, CreativeConcept,
    MediaPlan, KpiSnapshot, RoiReport, ComplianceVerdict, OptimizationSuggestion,
)
from adforge.guardrails.data_isolation import TenantScope, tenant_scope
from adforge.guardrails.exceptions import DataIsolationError
from adforge.guardrails.policy import PolicyEnforcer


class TestContracts:
    def test_campaign_brief_validates_budget(self, sample_brief_dict):
        brief = CampaignBrief(**sample_brief_dict)
        assert brief.budget_usd == 25000.0
        assert brief.business_objective == "conversion"

    def test_campaign_brief_rejects_zero_budget(self, sample_brief_dict):
        sample_brief_dict["budget_usd"] = 0
        with pytest.raises(Exception):
            CampaignBrief(**sample_brief_dict)

    def test_campaign_brief_rejects_unknown_objective(self, sample_brief_dict):
        sample_brief_dict["business_objective"] = " domination"  # not in literal
        with pytest.raises(Exception):
            CampaignBrief(**sample_brief_dict)

    def test_media_plan_validates_channel_allocations(self):
        plan = MediaPlan(
            channel_allocations=[{"channel": "google", "budget_usd": 1000.0}],
            total_budget_usd=1000.0,
            bidding_strategy="max_conversions",
            audience_targeting={"age": "25-44"},
            expected_reach=50000,
            expected_impressions=200000,
            flight_dates={"start": "2026-01-01", "end": "2026-02-01"},
        )
        assert plan.total_budget_usd == 1000.0

    def test_media_plan_rejects_missing_channel(self):
        with pytest.raises(Exception):
            MediaPlan(
                channel_allocations=[{"budget_usd": 1000.0}],  # no 'channel'
                total_budget_usd=1000.0,
                bidding_strategy="max_conversions",
                audience_targeting={},
                expected_reach=0,
                expected_impressions=0,
                flight_dates={"start": "2026-01-01", "end": "2026-02-01"},
            )

    def test_kpi_snapshot_validates_ctr_range(self):
        with pytest.raises(Exception):
            KpiSnapshot(
                channel="google", impressions=1000, clicks=2000, spend_usd=10,
                conversions=5, revenue_usd=50, ctr=2.0, cvr=0.1,
                cpc=0.05, cpa=2, cpm=10, roas=5,
            )

    def test_agent_input_requires_tenant(self):
        with pytest.raises(Exception):
            AgentInput(
                tenant_id="",  # empty
                campaign_id=uuid4(),
                agent_name="research",
            )


class TestDataIsolation:
    @pytest.mark.asyncio
    async def test_tenant_scope_context_manager(self):
        cid = uuid4()
        async with tenant_scope("tenant_a", cid) as scope:
            assert scope.tenant_id == "tenant_a"
            assert scope.campaign_id == cid
            assert scope.as_headers()["X-AdForge-Tenant"] == "tenant_a"

    @pytest.mark.asyncio
    async def test_tenant_scope_rejects_empty_tenant(self):
        with pytest.raises(DataIsolationError):
            async with tenant_scope("", uuid4()):
                pass

    def test_tenant_scope_assert_matches_raises(self):
        scope = TenantScope(tenant_id="a", campaign_id=uuid4())
        with pytest.raises(DataIsolationError):
            scope.assert_matches("b", "campaign:xyz")

    def test_tenant_scope_cache_key_prefix(self):
        scope = TenantScope(tenant_id="acme", campaign_id=uuid4())
        key = scope.cache_key("research", "v1")
        assert key.startswith("adforge:acme:")
        assert "research" in key


class TestPolicyEnforcer:
    def setup_method(self):
        self.enforcer = PolicyEnforcer.default()

    def test_clean_creative_approved(self):
        verdict = self.enforcer.evaluate("Build better software with Acme SaaS. Start free.")
        assert verdict.approved is True
        assert verdict.risk_level == "low"

    def test_tobacco_blocked(self):
        verdict = self.enforcer.evaluate("Buy our premium tobacco products today!")
        assert verdict.approved is False
        assert verdict.risk_level == "critical"
        assert "BLOCK_TOBACCO" in verdict.blocked_terms

    def test_financial_claim_requires_disclosure(self):
        verdict = self.enforcer.evaluate("Guaranteed return on your crypto investment!")
        assert verdict.risk_level in ("high", "critical")
        assert any("Investing involves risk" in d for d in verdict.required_disclosures)

    def test_sensitive_audience_targeting_blocked(self):
        verdict = self.enforcer.evaluate(
            "Great new product",
            audience_targeting={"race": "asian"},
        )
        assert verdict.approved is False
        assert any("TARGETING_RACE" in t for t in verdict.blocked_terms)

    def test_assert_approved_raises_when_blocked(self):
        from adforge.guardrails.exceptions import PolicyViolation
        with pytest.raises(PolicyViolation):
            self.enforcer.assert_approved("Buy our cigarettes online!")
