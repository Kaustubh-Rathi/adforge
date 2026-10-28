"""Agent eval runner — invokes each agent with a fixed test input and
checks the output conforms to the expected schema.

Used by CI to catch regressions. Two modes:
    --mock   (default in CI when no ZAI_API_KEY) — uses a deterministic mock
             LLM that returns canned JSON. Verifies schema plumbing only.
    (no flag) — calls the real LLM via the configured provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import uuid4

from adforge.agents.research.agent import ResearchAgent
from adforge.agents.creative_strategist.agent import CreativeStrategistAgent
from adforge.agents.media_planner.agent import MediaPlannerAgent
from adforge.agents.compliance.agent import ComplianceAgent
from adforge.agents.roi_calculator.agent import RoiCalculatorAgent
from adforge.agents.optimization.agent import OptimizationAgent
from adforge.guardrails.contracts import CampaignBrief


SAMPLE_BRIEF = {
    "product_name": "Acme CRM",
    "product_description": "Lightweight CRM for B2B sales teams. Tight Slack integration, AI-drafted follow-ups.",
    "target_audience": "Sales managers at 20-200 person B2B SaaS companies in the US.",
    "budget_usd": 12000.0,
    "duration_days": 21,
    "business_objective": "conversion",
    "channels_hint": ["google", "linkedin"],
    "constraints": ["No comparison-brand bidding"],
}


async def _mock_chat(messages, **kwargs):
    """Deterministic mock LLM for CI runs without an API key."""
    from adforge.llm.base import LLMResponse, LLMUsage
    user_msg = messages[-1]["content"] if messages else ""

    if "ResearchFindings" in user_msg:
        content = json.dumps({
            "market_summary": "B2B CRM market is $80B, growing 12% YoY. Remote-work adoption accelerates demand.",
            "top_keywords": ["best crm for b2b", "salesforce alternative", "lightweight crm"],
            "competitor_creatives": [{"brand": "Pipedrive", "headline": "Sales tool your team will love"}],
            "audience_persona": {"title": "Sales Manager", "pain_points": ["Too many tools", "Slow follow-ups"]},
            "trends": [{"name": "AI-assisted outreach", "momentum": "high"}],
            "sources": ["https://example.com/crm-market-2026"],
        })
    elif "CreativeConcept" in user_msg:
        content = json.dumps({
            "headline": "Stop losing deals to slow follow-ups",
            "primary_text": "Acme CRM drafts your follow-ups automatically. Slack-native, set up in 5 minutes.",
            "cta": "Start free",
            "visual_direction": "Split-screen: messy inbox on left, clean Acme dashboard on right. Bright accent color.",
            "ab_variants": [{"headline": "Never miss a follow-up again"}],
            "channel_adaptations": {"google": {"headline": "Acme CRM — Slack-native B2B CRM"}},
        })
    elif "MediaPlan" in user_msg:
        content = json.dumps({
            "channel_allocations": [
                {"channel": "google", "budget_usd": 8000.0, "expected_cpc": 3.5, "expected_cvr": 0.04},
                {"channel": "linkedin", "budget_usd": 4000.0, "expected_cpc": 8.0, "expected_cvr": 0.06},
            ],
            "total_budget_usd": 12000.0,
            "bidding_strategy": "max_conversions",
            "audience_targeting": {"age": "28-45", "industries": ["SaaS"]},
            "expected_reach": 60000,
            "expected_impressions": 250000,
            "flight_dates": {"start": "2026-02-01", "end": "2026-02-22"},
        })
    elif "ComplianceVerdict" in user_msg:
        content = json.dumps({
            "approved": True,
            "risk_level": "low",
            "flagged_issues": [],
            "required_disclosures": [],
            "reviewer_notes": "Creative is clean. No prohibited terms. Audience targeting OK.",
            "blocked_terms": [],
        })
    elif "RoiReport" in user_msg:
        content = json.dumps({
            "total_spend_usd": 4500, "total_revenue_usd": 13500,
            "total_conversions": 38, "blended_roas": 3.0, "blended_cpa": 118.4,
            "payback_period_days": 45, "lifetime_value_usd": 600,
            "net_profit_usd": 9000, "recommendation": "optimize",
            "rationale": "Google drove 85% of conversions at ROAS 4.1; LinkedIn underperformed at 1.2x.",
            "per_channel_breakdown": [{"channel": "google", "roas": 4.1}],
        })
    elif "OptimizationSuggestion" in user_msg:
        content = json.dumps({"suggestions": [
            {"channel": "google", "action": "increase_budget", "delta_pct": 0.30,
             "rationale": "ROAS 4.1x, CPA below target. Scale winner.",
             "expected_impact": {"cpa": "-8%", "roas": "+15%"}},
            {"channel": "linkedin", "action": "decrease_budget", "delta_pct": -0.50,
             "rationale": "ROAS 1.2x, CPA 3x target. Cut losses.",
             "expected_impact": {"cpa": "+20%", "spend": "-50%"}},
        ]})
    else:
        content = "{}"

    return LLMResponse(content=content, usage=LLMUsage(prompt_tokens=100, completion_tokens=200, total_tokens=300))


async def run_eval(mock: bool = False) -> int:
    """Run all agents on the sample brief. Returns 0 on success, 1 on any failure."""
    print(f"Running agent eval (mock={mock})...\n")

    if mock:
        # Patch the LLM factory to return a mock.
        from adforge.llm.factory import get_llm
        get_llm.cache_clear()
        # Replace the cached singleton with a stub that returns _mock_chat
        from adforge.llm.base import LLMProvider

        class _MockProvider(LLMProvider):
            async def chat(self, messages, **kw):
                return await _mock_chat(messages, **kw)

        import adforge.llm.factory as factory
        factory.get_llm = lambda: _MockProvider()  # type: ignore[assignment]

    brief = CampaignBrief(**SAMPLE_BRIEF)
    campaign_id = uuid4()
    failures: list[str] = []

    agents_to_test = [
        ("research", ResearchAgent(), {}),
        ("creative_strategist", CreativeStrategistAgent(), {"research": {}}),
        ("media_planner", MediaPlannerAgent(), {"research": {}, "creative": {}}),
        ("compliance", ComplianceAgent(), {"creative": {}, "media_plan": {}}),
        ("roi_calculator", RoiCalculatorAgent(), {"kpi_analytics": {}, "media_plan": {}}),
        ("optimization", OptimizationAgent(), {"kpi_analytics": {}, "roi_calculator": {}}),
    ]

    for name, agent, upstream in agents_to_test:
        print(f"  → {name}...", end=" ")
        try:
            result = await agent.run({
                "request_id": str(uuid4()),
                "tenant_id": "eval_tenant",
                "campaign_id": str(campaign_id),
                "agent_name": name,
                "brief": brief.model_dump(),
                "upstream_payloads": upstream,
            })
            if result.output.error:
                print(f"FAIL: {result.output.error}")
                failures.append(f"{name}: {result.output.error}")
            else:
                print(f"OK (payload keys: {list(result.output.payload.keys())})")
        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
            failures.append(f"{name}: {type(e).__name__}: {e}")

    print(f"\n{'='*40}")
    if failures:
        print(f"Eval FAILED — {len(failures)} agent(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Eval PASSED — all agents produced valid output.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Use mock LLM (no API key needed)")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_eval(mock=args.mock)))


if __name__ == "__main__":
    main()
