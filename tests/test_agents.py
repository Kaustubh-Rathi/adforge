"""Tests for the agent layer — message building + response parsing."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from adforge.agents.research.agent import ResearchAgent
from adforge.agents.creative_strategist.agent import CreativeStrategistAgent
from adforge.agents.media_planner.agent import MediaPlannerAgent
from adforge.agents.compliance.agent import ComplianceAgent
from adforge.agents.roi_calculator.agent import RoiCalculatorAgent
from adforge.agents.optimization.agent import OptimizationAgent
from adforge.guardrails.contracts import AgentInput, CampaignBrief


def _make_input(brief_dict, upstream=None):
    brief = CampaignBrief(**brief_dict)
    return AgentInput(
        tenant_id="test_tenant",
        campaign_id=uuid4(),
        agent_name="test",
        brief=brief,
        upstream_payloads=upstream or {},
    )


class TestResearchAgent:
    def test_build_messages_includes_persona_and_brief(self, sample_brief_dict):
        agent = ResearchAgent()
        inp = _make_input(sample_brief_dict)
        msgs = agent.build_messages(inp)
        assert msgs[0]["role"] == "system"
        assert "Research Agent" in msgs[0]["content"]
        assert sample_brief_dict["product_name"] in msgs[1]["content"]

    def test_parse_response_valid_json(self, sample_brief_dict):
        agent = ResearchAgent()
        inp = _make_input(sample_brief_dict)
        mock = {
            "market_summary": "The SaaS PM market is growing 18% YoY with strong tailwinds from remote-work adoption.",
            "top_keywords": ["best pm tool", "asana alternative"],
            "competitor_creatives": [{"brand": "Linear", "headline": "Issue tracking, redesigned"}],
            "audience_persona": {"title": "Engineering Manager"},
            "trends": [],
            "sources": ["https://example.com/market-report"],
        }
        payload = agent.parse_response(json.dumps(mock), inp)
        assert payload["market_summary"].startswith("The SaaS PM market")
        assert "best pm tool" in payload["top_keywords"]

    def test_parse_response_handles_markdown_fences(self, sample_brief_dict):
        agent = ResearchAgent()
        inp = _make_input(sample_brief_dict)
        mock = {
            "market_summary": "Test market summary that is at least 10 characters long.",
            "top_keywords": [], "sources": [],
        }
        fenced = "```json\n" + json.dumps(mock) + "\n```"
        payload = agent.parse_response(fenced, inp)
        assert payload["market_summary"].startswith("Test market")


class TestCreativeAgent:
    def test_build_messages_includes_research_upstream(self, sample_brief_dict):
        agent = CreativeStrategistAgent()
        inp = _make_input(sample_brief_dict, upstream={"research": {"top_keywords": ["x", "y"]}})
        msgs = agent.build_messages(inp)
        assert "Research findings" in msgs[1]["content"]


class TestMediaPlannerAgent:
    def test_parse_response_validates_budget_sum(self, sample_brief_dict):
        agent = MediaPlannerAgent()
        inp = _make_input(sample_brief_dict)
        mock = {
            "channel_allocations": [
                {"channel": "google", "budget_usd": 15000},
                {"channel": "linkedin", "budget_usd": 10000},
            ],
            "total_budget_usd": 25000,
            "bidding_strategy": "max_conversions",
            "audience_targeting": {"age": "25-44"},
            "expected_reach": 50000,
            "expected_impressions": 200000,
            "flight_dates": {"start": "2026-01-01", "end": "2026-02-01"},
        }
        payload = agent.parse_response(json.dumps(mock), inp)
        assert len(payload["channel_allocations"]) == 2


class TestComplianceAgent:
    def test_parse_response_overrides_when_policy_blocks(self, sample_brief_dict):
        agent = ComplianceAgent()
        inp = _make_input(
            sample_brief_dict,
            upstream={"creative": {"headline": "Buy cigarettes cheap",
                                    "primary_text": "Best tobacco deals",
                                    "cta": "Buy now"},
                      "media_plan": {"audience_targeting": {}}},
        )
        # LLM says approved; PolicyEnforcer should override.
        llm_output = {"approved": True, "risk_level": "low",
                       "flagged_issues": [], "required_disclosures": [],
                       "reviewer_notes": "Looks fine.", "blocked_terms": []}
        payload = agent.parse_response(json.dumps(llm_output), inp)
        assert payload["approved"] is False
        assert payload["risk_level"] == "critical"


class TestRoiCalculatorAgent:
    def test_parse_response_builds_report(self, sample_brief_dict):
        agent = RoiCalculatorAgent()
        inp = _make_input(sample_brief_dict)
        mock = {
            "total_spend_usd": 5000, "total_revenue_usd": 15000,
            "total_conversions": 50, "blended_roas": 3.0, "blended_cpa": 100,
            "net_profit_usd": 10000, "recommendation": "optimize",
            "rationale": "Google drove 80% of conversions; LinkedIn underperformed. Reallocate budget.",
            "per_channel_breakdown": [{"channel": "google", "roas": 4.2}],
        }
        payload = agent.parse_response(json.dumps(mock), inp)
        assert payload["recommendation"] == "optimize"
        assert payload["blended_roas"] == 3.0


class TestOptimizationAgent:
    def test_parse_response_caps_to_three_suggestions(self, sample_brief_dict):
        agent = OptimizationAgent()
        inp = _make_input(sample_brief_dict)
        mock = {"suggestions": [
            {"channel": "google", "action": "increase_budget", "delta_pct": 0.20,
             "rationale": "ROAS 4.5x, CPA below target.",
             "expected_impact": {"cpa": "-12%", "roas": "+18%"}},
            {"channel": "linkedin", "action": "pause", "delta_pct": -1.0,
             "rationale": "ROAS 0.3x, burning budget.",
             "expected_impact": {"spend": "-100%"}},
            {"channel": "meta", "action": "rotate_creative", "delta_pct": 0.0,
             "rationale": "CTR dropped 45% WoW.",
             "expected_impact": {"ctr": "+20%"}},
            {"channel": "tiktok", "action": "scale", "delta_pct": 0.50,
             "rationale": "Viral hook, scaling.",
             "expected_impact": {"reach": "+200%"}},
        ]}
        payload = agent.parse_response(json.dumps(mock), inp)
        assert len(payload["suggestions"]) == 3  # capped
