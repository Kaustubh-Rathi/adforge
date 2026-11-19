"""Graph state + node wiring."""

from __future__ import annotations

import asyncio
import logging
from operator import add
from typing import Annotated, Any, TypedDict, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from adforge.agents.compliance.agent import ComplianceAgent
from adforge.agents.creative_strategist.agent import CreativeStrategistAgent
from adforge.agents.kpi_analytics.agent import KpiAnalyticsAgent
from adforge.agents.media_planner.agent import MediaPlannerAgent
from adforge.agents.optimization.agent import OptimizationAgent
from adforge.agents.research.agent import ResearchAgent
from adforge.agents.roi_calculator.agent import RoiCalculatorAgent
from adforge.config import get_settings

logger = logging.getLogger("adforge.graph")


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
    review_status: str  # "pending" | "approved" | "rejected" | "skipped" | "auto_blocked"
    reviewer_email: str | None
    reviewer_comments: str | None
    approval_token: str | None
    revision_count: int  # number of times creative was revised after HITL reject
    kpi_analytics: dict[str, Any]
    roi_calculator: dict[str, Any]
    optimization: dict[str, Any]
    # ``add`` reducer: concurrent/sequential node updates append instead of
    # overwriting one another.
    errors: Annotated[list[str], add]


def thread_id_for(campaign_id: str | UUID) -> str:
    """Stable LangGraph checkpoint thread id for a campaign.

    The run and the HITL resume endpoints must agree on this value, otherwise
    ``Command(resume=...)`` has no checkpoint to resume from. Deriving it from
    the campaign id (instead of a per-run request id) makes that guarantee.
    """
    return f"campaign:{campaign_id}"


def graph_config(campaign_id: str | UUID) -> RunnableConfig:
    """LangGraph invocation config for a campaign's checkpoint thread.

    Typed so ``ainvoke``/``aget_state`` calls do not degrade to ``Any``; the
    run endpoint, the HITL resume endpoint and the eval runner must all pass
    the identical thread id.
    """
    return {"configurable": {"thread_id": thread_id_for(campaign_id)}}


# Singleton agent instances (cheap to construct; reuse across runs)
_research = ResearchAgent()
_creative = CreativeStrategistAgent()
_media_planner = MediaPlannerAgent()
_compliance = ComplianceAgent()
_kpi = KpiAnalyticsAgent()
_roi = RoiCalculatorAgent()
_optimization = OptimizationAgent()


def _agent_input(state: CampaignState, agent_name: str, upstream: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": state.get("request_id", str(uuid4())),
        "tenant_id": state["tenant_id"],
        "campaign_id": state["campaign_id"],
        "agent_name": agent_name,
        "brief": state.get("brief"),
        "upstream_payloads": upstream,
    }


# --------------------------------------------------------------------------- #
# Node functions — each wraps an agent's run() and updates state.
# --------------------------------------------------------------------------- #


async def research_node(state: CampaignState) -> dict[str, Any]:
    result = await _research.run(_agent_input(state, "research", {}))
    if result.output.error is None:
        return {"research": result.output.payload}
    return {"research": {}, "errors": [f"research: {result.output.error}"]}


async def creative_node(state: CampaignState) -> dict[str, Any]:
    upstream: dict[str, Any] = {"research": state.get("research", {})}
    # Pass the reviewer's rejection comments into the revision round.
    if state.get("reviewer_comments"):
        upstream["reviewer_comments"] = state["reviewer_comments"]
        upstream["revision_count"] = state.get("revision_count", 0)

    result = await _creative.run(_agent_input(state, "creative_strategist", upstream))
    if result.output.error is None:
        return {"creative": result.output.payload}
    return {"creative": {}, "errors": [f"creative: {result.output.error}"]}


async def media_planner_node(state: CampaignState) -> dict[str, Any]:
    result = await _media_planner.run(
        _agent_input(
            state,
            "media_planner",
            {
                "research": state.get("research", {}),
                "creative": state.get("creative", {}),
            },
        )
    )
    if result.output.error is None:
        return {"media_plan": result.output.payload}
    return {"media_plan": {}, "errors": [f"media_planner: {result.output.error}"]}


async def compliance_node(state: CampaignState) -> dict[str, Any]:
    result = await _compliance.run(
        _agent_input(
            state,
            "compliance",
            {
                "creative": state.get("creative", {}),
                "media_plan": state.get("media_plan", {}),
            },
        )
    )
    if result.output.error is None:
        return {"compliance_verdict": result.output.payload}
    # A failed compliance review must never be treated as approval.
    return {
        "compliance_verdict": {"approved": False, "risk_level": "critical"},
        "errors": [f"compliance: {result.output.error}"],
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
    state. The orchestrator's publish step uses this token to call the ads
    MCP's @write_operation tools.
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
            reviewer_email=reviewer_verdict.get("reviewer_email") or "unknown",
            ttl_seconds=3600,
            tenant_id=state["tenant_id"],
        )
        return {
            "review_status": "approved",
            "reviewer_email": reviewer_verdict.get("reviewer_email"),
            "reviewer_comments": reviewer_verdict.get("comments"),
            "approval_token": token,
        }

    return {
        "review_status": "rejected",
        "reviewer_email": reviewer_verdict.get("reviewer_email"),
        "reviewer_comments": reviewer_verdict.get("comments") or "No comment provided.",
        "revision_count": state.get("revision_count", 0) + 1,
    }


async def kpi_analytics_node(state: CampaignState) -> dict[str, Any]:
    result = await _kpi.run(
        _agent_input(
            state,
            "kpi_analytics",
            {"media_plan": state.get("media_plan", {})},
        )
    )
    if result.output.error is None:
        return {"kpi_analytics": result.output.payload}
    return {"kpi_analytics": {}, "errors": [f"kpi_analytics: {result.output.error}"]}


async def roi_calculator_node(state: CampaignState) -> dict[str, Any]:
    result = await _roi.run(
        _agent_input(
            state,
            "roi_calculator",
            {
                "kpi_analytics": state.get("kpi_analytics", {}),
                "media_plan": state.get("media_plan", {}),
            },
        )
    )
    if result.output.error is None:
        return {"roi_calculator": result.output.payload}
    return {"roi_calculator": {}, "errors": [f"roi_calculator: {result.output.error}"]}


async def optimization_node(state: CampaignState) -> dict[str, Any]:
    result = await _optimization.run(
        _agent_input(
            state,
            "optimization",
            {
                "kpi_analytics": state.get("kpi_analytics", {}),
                "roi_calculator": state.get("roi_calculator", {}),
                "media_plan": state.get("media_plan", {}),
            },
        )
    )
    if result.output.error is None:
        return {"optimization": result.output.payload}
    return {"optimization": {}, "errors": [f"optimization: {result.output.error}"]}


# --------------------------------------------------------------------------- #
# Edge routing (v0.2 — adds HITL review branching)
# --------------------------------------------------------------------------- #


def route_after_compliance(state: CampaignState) -> str:
    """v0.2: route to human_review node (no longer directly to kpi_analytics)."""
    return "human_review"


def route_after_review(state: CampaignState) -> str:
    """After the HITL review interrupt resumes:

    - approved     → kpi_analytics
    - rejected     → creative_strategist (revise, if revision_count < 3)
    - rejected (max revisions hit) → END (campaign blocked)
    - skipped      → kpi_analytics (dev mode)
    - auto_blocked → END (compliance critical risk)
    """
    status = state.get("review_status", "pending")
    if status in ("approved", "skipped"):
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


def build_graph() -> StateGraph[CampaignState]:
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


_compiled_graph: CompiledStateGraph[Any, Any, Any, Any] | None = None
_graph_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _graph_lock
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()
    return _graph_lock


def _checkpointer_conninfo(database_url: str) -> str:
    """Return a psycopg DSN for the configured database URL.

    ``localhost`` can resolve to ``::1`` before ``127.0.0.1``. Local Docker
    publishes Postgres on IPv4 loopback only, and psycopg tries resolved
    addresses sequentially, so the IPv6 attempt can consume the entire connect
    timeout. Rewrite the loopback name to avoid that stall; other hosts are
    left untouched.
    """
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    if (url.host or "").lower() == "localhost":
        url = url.set(host="127.0.0.1")
    return url.render_as_string(hide_password=False)


async def _build_checkpointer() -> BaseCheckpointSaver[Any] | None:
    """Construct the configured checkpointer.

    Postgres gives durable HITL resumes across API workers/restarts. In
    staging/prod a Postgres failure is fatal: silently falling back to an
    in-process MemorySaver would lose the approval checkpoint and could let a
    paused campaign resume against a different state. Dev/test may fall back
    to MemorySaver for convenience.
    """
    from langgraph.checkpoint.memory import MemorySaver

    settings = get_settings()
    backend = settings.checkpointer_backend
    strict = settings.app_env in ("staging", "prod")

    if backend == "none":
        if strict:
            raise RuntimeError(
                "CHECKPOINTER_BACKEND=none is unsafe in staging/prod: HITL "
                "approval state would not be durable"
            )
        return None
    if backend == "memory":
        if strict:
            raise RuntimeError(
                "CHECKPOINTER_BACKEND=memory is unsafe in staging/prod: HITL "
                "approval state would not survive a restart"
            )
        return MemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        conninfo = _checkpointer_conninfo(settings.database_url)
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        # ``setup()`` uses ``pool.connection()``, which raises PoolClosed when
        # the pool was constructed with ``open=False`` and never opened.
        await pool.open(wait=True, timeout=10)
        try:
            # psycopg's pool is generic over its row factory (a tuple by
            # default) while ``AsyncPostgresSaver`` is written against dict
            # rows. The runtime object is the same pool; only the static
            # parameter differs, so narrow it here instead of at the builder.
            checkpointer = AsyncPostgresSaver(cast("Any", pool))
            await checkpointer.setup()
        except Exception:
            await pool.close()
            raise
        logger.info("LangGraph checkpointer: Postgres")
        return checkpointer
    except Exception as exc:
        if strict:
            raise RuntimeError(
                "Postgres checkpointer is required in staging/prod but could "
                "not be initialised; refusing to start with non-durable HITL "
                "state"
            ) from exc
        logger.warning(
            "Postgres checkpointer unavailable — falling back to MemorySaver "
            "(HITL resumes will not survive a restart)",
            exc_info=True,
        )
        return MemorySaver()


async def get_compiled_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """Return a compiled + checkpointer-wired graph (async singleton)."""
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    async with _lock():
        if _compiled_graph is not None:
            return _compiled_graph
        builder = build_graph()
        checkpointer = await _build_checkpointer()
        _compiled_graph = (
            builder.compile(checkpointer=checkpointer)
            if checkpointer is not None
            else builder.compile()
        )
        return _compiled_graph


async def clear_thread(campaign_id: str | UUID) -> None:
    """Delete a campaign's checkpoint so a re-run starts from a clean state.

    Without this, re-running an already-approved campaign would resume the
    old checkpoint and could expose or reuse a stale ``approval_token``.
    """
    graph = await get_compiled_graph()
    checkpointer = getattr(graph, "checkpointer", None)
    deleter = getattr(checkpointer, "adelete_thread", None)
    if deleter is not None:
        await deleter(thread_id_for(campaign_id))


def reset_compiled_graph() -> None:
    """Drop the compiled graph singleton. Used by tests."""
    global _compiled_graph
    _compiled_graph = None
