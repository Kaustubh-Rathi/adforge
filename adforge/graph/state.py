"""Graph state + node wiring."""
from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt, Command

from adforge.agents.research.agent import ResearchAgent
from adforge.agents.creative_strategist.agent import CreativeStrategistAgent
from adforge.agents.media_planner.agent import MediaPlannerAgent
from adforge.agents.kpi_analytics.agent import KpiAnalyticsAgent
from adforge.agents.roi_calculator.agent import RoiCalculatorAgent
from adforge.agents.compliance.agent import ComplianceAgent
from adforge.agents.optimization.agent import OptimizationAgent
from adforge.config import get_settings


class CampaignState(TypedDict, total=False):
    """Graph state passed through every agent node."""

    request_id: str
    tenant_id: str
    campaign_id: str
    brief: dict[str, Any]
    research: dict[str, Any]
    creative: dict[str, Any]
    media_plan: dict[str, Any]
    compliance_verdict: dict[str, Any]
    # v0.2: HITL review state
    review_required: bool
    review_status: str  # "pending" | "approved" | "rejected"
    reviewer_email: str | None
    reviewer_comments: str | None
    approval_token: str | None
    revision_count: int  # number of times creative was revised after HITL reject
    kpi_analytics: dict[str, Any]
    roi_calculator: dict[str, Any]
    optimization: dict[str, Any]
    errors: list[str]


# Singleton agent instances (cheap to construct; reuse across runs)
_research = ResearchAgent()
_creative = CreativeStrategistAgent()
_media_planner = MediaPlannerAgent()
_compliance = ComplianceAgent()
_kpi = KpiAnalyticsAgent()
_roi = RoiCalculatorAgent()
_optimization = OptimizationAgent()


# --------------------------------------------------------------------------- #
# Node functions — each wraps an agent's run() and updates state.
# --------------------------------------------------------------------------- #


async def research_node(state: CampaignState) -> dict[str, Any]:
    result = await _research.run({
        "request_id": state.get("request_id", str(uuid4())),
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "research",
        "brief": state.get("brief"),
        "upstream_payloads": {},
    })
    return {"research": result.output.payload} if result.output.error is None else {
        "research": {},
        "errors": state.get("errors", []) + [f"research: {result.output.error}"],
    }


async def creative_node(state: CampaignState) -> dict[str, Any]:
    result = await _creative.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "creative_strategist",
        "brief": state.get("brief"),
        "upstream_payloads": {"research": state.get("research", {})},
    })
    return {"creative": result.output.payload} if result.output.error is None else {
        "creative": {},
        "errors": state.get("errors", []) + [f"creative: {result.output.error}"],
    }


async def media_planner_node(state: CampaignState) -> dict[str, Any]:
    result = await _media_planner.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "media_planner",
        "brief": state.get("brief"),
        "upstream_payloads": {
            "research": state.get("research", {}),
            "creative": state.get("creative", {}),
        },
    })
    return {"media_plan": result.output.payload} if result.output.error is None else {
        "media_plan": {},
        "errors": state.get("errors", []) + [f"media_planner: {result.output.error}"],
    }


async def compliance_node(state: CampaignState) -> dict[str, Any]:
    result = await _compliance.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "compliance",
        "brief": state.get("brief"),
        "upstream_payloads": {
            "creative": state.get("creative", {}),
            "media_plan": state.get("media_plan", {}),
        },
    })
    return {"compliance_verdict": result.output.payload} if result.output.error is None else {
        "compliance_verdict": {"approved": False, "risk_level": "critical"},
        "errors": state.get("errors", []) + [f"compliance: {result.output.error}"],
    }


# --------------------------------------------------------------------------- #
# v0.2: Human-in-the-Loop review node
# --------------------------------------------------------------------------- #


async def human_review_node(state: CampaignState) -> dict[str, Any]:
    """Pause the graph and surface the creative + media plan + compliance
    verdict to a human reviewer.

    Uses LangGraph's ``interrupt()`` mechanism. The graph is persisted to
    the Postgres checkpointer; a separate API endpoint resumes it with
    either an approve or reject Command.

    On reject, the reviewer's comments are passed to ``creative_strategist``
    via ``upstream_payloads["reviewer_comments"]`` and the graph re-runs
    creative → media_planner → compliance → human_review (max 3 revisions).

    On approve, an HMAC-signed approval token is generated and stored in
    state. The orchestrator's publish step (not yet wired in v0.2) uses
    this token to call the ads MCP's @write_operation tools.
    """
    settings = get_settings()
    verdict = state.get("compliance_verdict", {})

    # If compliance auto-rejected (critical risk), skip HITL and end with blocked status.
    if verdict.get("risk_level") == "critical" and not verdict.get("approved"):
        return {
            "review_status": "auto_blocked",
            "reviewer_comments": "Auto-blocked by Compliance (critical risk). No human review performed.",
        }

    # If guardrails say we don't require HITL (e.g. dev mode), skip the interrupt.
    if not settings.guardrails_require_compliance_approval:
        return {"review_status": "skipped"}

    # Build the review packet — what the reviewer sees in the dashboard.
    review_packet = {
        "campaign_id": state.get("campaign_id"),
        "creative": state.get("creative", {}),
        "media_plan": state.get("media_plan", {}),
        "compliance_verdict": verdict,
        "revision_count": state.get("revision_count", 0),
        "prompt": "Approve this creative + media plan to proceed to launch?",
    }

    # interrupt() pauses execution here. The caller (API endpoint) resumes
    # by calling ``graph.ainvoke(Command(resume=<verdict>), config)`` with
    # ``verdict = {"approved": bool, "reviewer_email": str, "comments": str}``.
    reviewer_verdict: dict[str, Any] = interrupt(review_packet)

    if reviewer_verdict.get("approved"):
        # Issue an HMAC-signed approval token valid for 1 hour.
        from adforge.mcp_servers.permissions import issue_approval_token
        token = issue_approval_token(
            campaign_id=state["campaign_id"],
            operation="publish_campaign",
            reviewer_email=reviewer_verdict.get("reviewer_email", "unknown"),
            ttl_seconds=3600,
        )
        return {
            "review_status": "approved",
            "reviewer_email": reviewer_verdict.get("reviewer_email"),
            "reviewer_comments": reviewer_verdict.get("comments"),
            "approval_token": token,
        }
    else:
        return {
            "review_status": "rejected",
            "reviewer_email": reviewer_verdict.get("reviewer_email"),
            "reviewer_comments": reviewer_verdict.get("comments", "No comment provided."),
            "revision_count": state.get("revision_count", 0) + 1,
        }


async def kpi_analytics_node(state: CampaignState) -> dict[str, Any]:
    result = await _kpi.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "kpi_analytics",
        "brief": state.get("brief"),
        "upstream_payloads": {
            "media_plan": state.get("media_plan", {}),
        },
    })
    return {"kpi_analytics": result.output.payload} if result.output.error is None else {
        "kpi_analytics": {},
        "errors": state.get("errors", []) + [f"kpi_analytics: {result.output.error}"],
    }


async def roi_calculator_node(state: CampaignState) -> dict[str, Any]:
    result = await _roi.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "roi_calculator",
        "brief": state.get("brief"),
        "upstream_payloads": {
            "kpi_analytics": state.get("kpi_analytics", {}),
            "media_plan": state.get("media_plan", {}),
        },
    })
    return {"roi_calculator": result.output.payload} if result.output.error is None else {
        "roi_calculator": {},
        "errors": state.get("errors", []) + [f"roi_calculator: {result.output.error}"],
    }


async def optimization_node(state: CampaignState) -> dict[str, Any]:
    result = await _optimization.run({
        "request_id": state["request_id"],
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": "optimization",
        "brief": state.get("brief"),
        "upstream_payloads": {
            "kpi_analytics": state.get("kpi_analytics", {}),
            "roi_calculator": state.get("roi_calculator", {}),
            "media_plan": state.get("media_plan", {}),
        },
    })
    return {"optimization": result.output.payload} if result.output.error is None else {
        "optimization": {},
        "errors": state.get("errors", []) + [f"optimization: {result.output.error}"],
    }


# --------------------------------------------------------------------------- #
# Edge routing (v0.2 — adds HITL review branching)
# --------------------------------------------------------------------------- #


def route_after_compliance(state: CampaignState) -> str:
    """v0.2: route to human_review node (no longer directly to kpi_analytics)."""
    return "human_review"


def route_after_review(state: CampaignState) -> str:
    """After the HITL review interrupt resumes:
       - approved    → kpi_analytics
       - rejected    → creative_strategist (revise, if revision_count < 3)
       - rejected (max revisions hit) → END (campaign blocked)
       - skipped     → kpi_analytics (dev mode)
       - auto_blocked → END (compliance critical risk)
    """
    status = state.get("review_status", "pending")
    if status == "approved" or status == "skipped":
        return "kpi_analytics"
    if status == "auto_blocked":
        return END
    if status == "rejected":
        if state.get("revision_count", 0) >= 3:
            return END  # too many revisions — give up
        return "creative_strategist"
    return "kpi_analytics"


def route_after_creative_revision(state: CampaignState) -> str:
    """When creative is re-run after a HITL reject, route back through media_planner
    so the new creative + the same budget get re-evaluated."""
    return "media_planner"


# --------------------------------------------------------------------------- #
# Graph builder (v0.2 — adds human_review node)
# --------------------------------------------------------------------------- #


def build_graph():
    """Construct the LangGraph StateGraph (not yet compiled)."""
    builder = StateGraph(CampaignState)
    builder.add_node("research", research_node)
    builder.add_node("creative_strategist", creative_node)
    builder.add_node("media_planner", media_planner_node)
    builder.add_node("compliance", compliance_node)
    builder.add_node("human_review", human_review_node)  # v0.2
    builder.add_node("kpi_analytics", kpi_analytics_node)
    builder.add_node("roi_calculator", roi_calculator_node)
    builder.add_node("optimization", optimization_node)

    builder.add_edge(START, "research")
    builder.add_edge("research", "creative_strategist")
    builder.add_edge("creative_strategist", "media_planner")
    builder.add_edge("media_planner", "compliance")
    builder.add_conditional_edges(
        "compliance",
        route_after_compliance,
        {"human_review": "human_review"},
    )
    builder.add_conditional_edges(
        "human_review",
        route_after_review,
        {
            "kpi_analytics": "kpi_analytics",
            "creative_strategist": "creative_strategist",
            END: END,
        },
    )
    builder.add_edge("kpi_analytics", "roi_calculator")
    builder.add_edge("roi_calculator", "optimization")
    builder.add_edge("optimization", END)
    return builder


_compiled_graph = None


def get_compiled_graph():
    """Return a compiled + checkpointer-wired graph (singleton)."""
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    builder = build_graph()
    s = get_settings()

    # Wire up Postgres checkpointer if we have a DB.
    # In test mode without a DB, compile without checkpointing.
    try:
        pool = AsyncConnectionPool(
            conninfo=s.database_url.replace("postgresql+asyncpg://", "postgresql://"),
            min_size=1, max_size=5, open=False,
        )
        checkpointer = AsyncPostgresSaver(pool)
        # pool is opened lazily on first use — don't call .open() here
        _compiled_graph = builder.compile(checkpointer=checkpointer)
    except Exception:
        # No DB available — fall back to in-memory graph (no resume capability)
        _compiled_graph = builder.compile()
    return _compiled_graph
