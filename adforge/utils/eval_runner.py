"""Agent eval runner — exercises every agent (and the full graph) against a
fixed brief, then guards the numbers.

Three kinds of checks:

1. Schema: every agent output is validated against its Pydantic contract.
2. Metric recomputation: KPI ratios and ROI totals are recomputed from the raw
   counts and compared to what the agent reported. This catches fabricated or
   drifted numbers that still satisfy the schema.
3. End-to-end: the LangGraph topology runs research → … → optimization.

The runner always uses the configured live LLM and real ad-platform reporting
APIs. There is no simulated-data mode: missing credentials surface as eval
failures so an operator knows exactly what is unconfigured.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any
from uuid import uuid4

from adforge.agents.compliance.agent import ComplianceAgent
from adforge.agents.creative_strategist.agent import CreativeStrategistAgent
from adforge.agents.kpi_analytics.agent import KpiAnalyticsAgent
from adforge.agents.media_planner.agent import MediaPlannerAgent
from adforge.agents.research.agent import ResearchAgent
from adforge.agents.roi_calculator.agent import RoiCalculatorAgent
from adforge.guardrails.contracts import CampaignBrief, KpiSnapshot, RoiReport
from adforge.llm.base import LLMProvider

SAMPLE_BRIEF = {
    "product_name": "Acme CRM",
    "product_description": "Lightweight CRM for B2B sales teams. Tight Slack integration, AI-drafted follow-ups.",
    "target_audience": "Sales managers at 20-200 person B2B SaaS companies in the US.",
    "budget_usd": 25000.0,
    "duration_days": 21,
    "business_objective": "conversion",
    "channels_hint": ["google", "linkedin"],
    "constraints": ["No comparison-brand bidding"],
}


# --------------------------------------------------------------------------- #
# Metric guards
# --------------------------------------------------------------------------- #


def _close(actual: float, expected: float, tolerance: float = 0.02) -> bool:
    return abs(actual - expected) <= tolerance * max(abs(expected), 1.0)


def check_kpi_metrics(snapshots: list[dict[str, Any]]) -> list[str]:
    """Recompute KPI ratios from raw counts; flag mismatches."""
    failures: list[str] = []
    for row in snapshots:
        # KpiSnapshot itself forbids negative money, so flag it before schema
        # validation would raise instead of producing an eval failure.
        if row.get("spend_usd", 0) < 0 or row.get("revenue_usd", 0) < 0:
            failures.append(f"{row.get('channel', '?')}: negative money value")
            continue
        snap = KpiSnapshot(**row)
        expected_ctr = snap.clicks / snap.impressions if snap.impressions else 0.0
        expected_cvr = snap.conversions / snap.clicks if snap.clicks else 0.0
        expected_roas = snap.revenue_usd / snap.spend_usd if snap.spend_usd else 0.0
        if not _close(snap.ctr, expected_ctr, 0.01):
            failures.append(f"{snap.channel}: ctr {snap.ctr} != {expected_ctr:.4f}")
        if not _close(snap.cvr, expected_cvr, 0.01):
            failures.append(f"{snap.channel}: cvr {snap.cvr} != {expected_cvr:.4f}")
        if not _close(snap.roas, expected_roas, 0.01):
            failures.append(f"{snap.channel}: roas {snap.roas} != {expected_roas:.2f}")
        if snap.spend_usd < 0 or snap.revenue_usd < 0:
            failures.append(f"{snap.channel}: negative money value")
    return failures


def check_roi_metrics(report: dict[str, Any]) -> list[str]:
    """Recompute ROI totals/ratios from the raw spend + revenue."""
    roi = RoiReport(**report)
    failures: list[str] = []
    expected_roas = (
        roi.total_revenue_usd / roi.total_spend_usd if roi.total_spend_usd else 0.0
    )
    expected_cpa = (
        roi.total_spend_usd / roi.total_conversions if roi.total_conversions else 0.0
    )
    expected_profit = roi.total_revenue_usd - roi.total_spend_usd
    if not _close(roi.blended_roas, expected_roas, 0.02):
        failures.append(f"blended_roas {roi.blended_roas} != {expected_roas:.2f}")
    if not _close(roi.blended_cpa, expected_cpa, 0.02):
        failures.append(f"blended_cpa {roi.blended_cpa} != {expected_cpa:.2f}")
    if not _close(roi.net_profit_usd, expected_profit, 0.01):
        failures.append(f"net_profit {roi.net_profit_usd} != {expected_profit:.2f}")
    return failures


# --------------------------------------------------------------------------- #
# Eval execution
# --------------------------------------------------------------------------- #


async def run_eval(
    provider: LLMProvider | None = None,
    *,
    include_graph: bool = True,
) -> int:
    """Run all agents on the sample brief. Returns 0 on success, 1 on failure."""
    print(
        "Running agent eval "
        f"(provider={'injected' if provider is not None else 'configured'})\n"
    )

    brief = CampaignBrief(**SAMPLE_BRIEF)
    campaign_id = uuid4()
    failures: list[str] = []

    research = ResearchAgent(llm=provider)
    creative = CreativeStrategistAgent(llm=provider)
    media_planner = MediaPlannerAgent(llm=provider)
    compliance = ComplianceAgent()
    compliance._llm = provider
    kpi = KpiAnalyticsAgent()
    roi = RoiCalculatorAgent(llm=provider)

    async def run_agent(name, agent, upstream):
        print(f"  -> {name}...", end=" ")
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
            return {}
        print(f"OK ({len(result.output.payload)} payload keys)")
        return result.output.payload

    research_payload = await run_agent("research", research, {})
    creative_payload = await run_agent("creative_strategist", creative, {
        "research": research_payload,
    })
    media_payload = await run_agent("media_planner", media_planner, {
        "research": research_payload,
        "creative": creative_payload,
    })
    compliance_payload = await run_agent("compliance", compliance, {
        "creative": creative_payload,
        "media_plan": media_payload,
    })
    kpi_payload = await run_agent("kpi_analytics", kpi, {
        "media_plan": media_payload,
    })
    roi_payload = await run_agent("roi_calculator", roi, {
        "kpi_analytics": kpi_payload,
        "media_plan": media_payload,
    })

    # Guard the numbers the agents reported.
    for failure in check_kpi_metrics(kpi_payload.get("snapshots", [])):
        failures.append(f"kpi_metrics: {failure}")
    # A failed ROI agent already contributed a failure above; don't crash the
    # eval trying to validate an empty payload.
    if roi_payload:
        for failure in check_roi_metrics(roi_payload):
            failures.append(f"roi_metrics: {failure}")
    if not compliance_payload:
        failures.append("compliance: empty verdict")
    if not kpi_payload.get("snapshots"):
        failures.append("kpi_analytics: no snapshots returned")

    if include_graph:
        print("  -> graph...", end=" ")
        graph_failures = await _run_graph_eval(provider)
        if graph_failures:
            print(f"FAIL: {graph_failures}")
            failures.extend(f"graph: {item}" for item in graph_failures)
        else:
            print("OK")

    print(f"\n{'=' * 40}")
    if failures:
        print(f"Eval FAILED - {len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Eval PASSED - all agents produced valid, self-consistent output.")
    return 0


async def _run_graph_eval(provider: LLMProvider | None) -> list[str]:
    """Run the graph end-to-end without HITL pausing (eval mode only)."""
    import adforge.graph.state as graph_state
    from adforge.config import get_settings

    settings = get_settings()
    previous = settings.guardrails_require_compliance_approval
    try:
        # The eval exercises topology, not the review queue.
        settings.guardrails_require_compliance_approval = False
        for agent in (
            graph_state._research,
            graph_state._creative,
            graph_state._media_planner,
            graph_state._compliance,
            graph_state._roi,
            graph_state._optimization,
        ):
            if provider is not None:
                agent._llm = provider

        from langgraph.checkpoint.memory import MemorySaver

        campaign_id = uuid4()
        compiled = graph_state.build_graph().compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": graph_state.thread_id_for(campaign_id)}}
        final = await compiled.ainvoke(
            {
                "request_id": str(uuid4()),
                "tenant_id": "eval_tenant",
                "campaign_id": str(campaign_id),
                "brief": CampaignBrief(**SAMPLE_BRIEF).model_dump(),
                "errors": [],
            },
            config=config,
        )
    except Exception as e:  # noqa: BLE001 — eval reports, doesn't crash
        return [f"{type(e).__name__}: {e}"]
    finally:
        settings.guardrails_require_compliance_approval = previous

    problems: list[str] = []
    if final.get("errors"):
        problems.extend(final["errors"])
    if not final.get("optimization"):
        problems.append("optimization payload missing")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-graph", action="store_true", help="Skip the graph eval")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_eval(include_graph=not args.skip_graph)))


if __name__ == "__main__":
    main()
