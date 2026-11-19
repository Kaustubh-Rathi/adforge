"""Unit tests for the FastAPI surface (no DB) and the LangGraph wiring."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import adforge.graph.state as graph_state
from adforge.guardrails.contracts import CampaignBrief

# --------------------------------------------------------------------------- #
# API surface that doesn't touch the database
# --------------------------------------------------------------------------- #


@pytest.fixture
async def bare_client():
    from adforge.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://adforge.test") as client:
        yield client


class TestApiMetadata:
    async def test_health_endpoint(self, bare_client):
        r = await bare_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_root_endpoint_lists_agents(self, bare_client):
        r = await bare_client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["agents"], list)
        assert {"research", "roi_calculator", "compliance"}.issubset(set(data["agents"]))

    async def test_campaigns_require_tenant_header(self, bare_client):
        r = await bare_client.get("/campaigns")
        assert r.status_code == 400
        assert "X-AdForge-Tenant" in r.json()["detail"]

    async def test_reviews_require_tenant_header(self, bare_client):
        r = await bare_client.get("/reviews/pending")
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Graph topology + routing
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, payload=None, error=None):
        self.output = SimpleNamespace(payload=payload or {}, error=error)


def _state(**overrides):
    return {
        "request_id": str(uuid4()),
        "tenant_id": "tenant_a",
        "campaign_id": str(uuid4()),
        "brief": {},
        **overrides,
    }


class TestGraphTopology:
    def test_graph_builds_with_all_nodes(self):
        builder = graph_state.build_graph()
        expected = {
            "research",
            "creative_strategist",
            "media_planner",
            "compliance",
            "human_review",
            "kpi_analytics",
            "roi_calculator",
            "optimization",
        }
        assert expected == set(builder.nodes.keys())

    def test_thread_id_is_stable_and_campaign_scoped(self):
        cid = uuid4()
        assert graph_state.thread_id_for(cid) == f"campaign:{cid}"
        assert graph_state.thread_id_for(cid) == graph_state.thread_id_for(str(cid))

    def test_route_after_compliance_always_human_review(self):
        assert (
            graph_state.route_after_compliance({"compliance_verdict": {"approved": True}})
            == "human_review"
        )
        assert (
            graph_state.route_after_compliance(
                {"compliance_verdict": {"approved": False, "risk_level": "critical"}}
            )
            == "human_review"
        )

    @pytest.mark.parametrize(
        "state,expected",
        [
            ({"review_status": "approved"}, "kpi_analytics"),
            ({"review_status": "skipped"}, "kpi_analytics"),
            ({"review_status": "pending"}, "kpi_analytics"),
            ({"review_status": "auto_blocked"}, "__end__"),
            ({"review_status": "rejected", "revision_count": 1}, "creative_strategist"),
            ({"review_status": "rejected", "revision_count": 3}, "__end__"),
        ],
    )
    def test_route_after_review(self, state, expected):
        assert graph_state.route_after_review(state) == expected

    def test_route_after_creative_revision(self):
        assert graph_state.route_after_creative_revision({}) == "media_planner"

    def test_agent_input_fills_request_id_and_upstream(self):
        state = _state()
        payload = graph_state._agent_input(state, "research", {"x": 1})
        assert payload["request_id"] == state["request_id"]
        assert payload["tenant_id"] == "tenant_a"
        assert payload["agent_name"] == "research"
        assert payload["upstream_payloads"] == {"x": 1}

    def test_agent_input_generates_request_id_when_missing(self):
        payload = graph_state._agent_input(
            {"tenant_id": "t", "campaign_id": str(uuid4())}, "research", {}
        )
        assert payload["request_id"]


class TestGraphNodes:
    async def test_research_node_success(self, monkeypatch):
        async def fake_run(raw):
            return _FakeResult({"market_summary": "ok"})

        monkeypatch.setattr(graph_state._research, "run", fake_run)
        out = await graph_state.research_node(_state())
        assert out == {"research": {"market_summary": "ok"}}

    async def test_research_node_error(self, monkeypatch):
        async def fake_run(raw):
            return _FakeResult(error="boom")

        monkeypatch.setattr(graph_state._research, "run", fake_run)
        out = await graph_state.research_node(_state())
        assert out["research"] == {}
        assert out["errors"] == ["research: boom"]

    async def test_creative_node_passes_reviewer_comments(self, monkeypatch):
        captured = {}

        async def fake_run(raw):
            captured.update(raw)
            return _FakeResult({"headline": "new"})

        monkeypatch.setattr(graph_state._creative, "run", fake_run)
        out = await graph_state.creative_node(
            _state(reviewer_comments="too generic", revision_count=1)
        )
        assert captured["upstream_payloads"]["reviewer_comments"] == "too generic"
        assert captured["upstream_payloads"]["revision_count"] == 1
        assert out == {"creative": {"headline": "new"}}

    async def test_creative_node_error(self, monkeypatch):
        async def fake_run(raw):
            return _FakeResult(error="bad json")

        monkeypatch.setattr(graph_state._creative, "run", fake_run)
        out = await graph_state.creative_node(_state())
        assert out["errors"] == ["creative: bad json"]

    async def test_media_planner_node_success_and_error(self, monkeypatch):
        async def ok(raw):
            return _FakeResult({"total_budget_usd": 1})

        monkeypatch.setattr(graph_state._media_planner, "run", ok)
        assert (await graph_state.media_planner_node(_state()))["media_plan"] == {
            "total_budget_usd": 1
        }

        async def bad(raw):
            return _FakeResult(error="nope")

        monkeypatch.setattr(graph_state._media_planner, "run", bad)
        out = await graph_state.media_planner_node(_state())
        assert out["media_plan"] == {}
        assert out["errors"] == ["media_planner: nope"]

    async def test_compliance_node_failure_never_approves(self, monkeypatch):
        async def bad(raw):
            return _FakeResult(error="LLM down")

        monkeypatch.setattr(graph_state._compliance, "run", bad)
        out = await graph_state.compliance_node(_state())
        assert out["compliance_verdict"]["approved"] is False
        assert out["compliance_verdict"]["risk_level"] == "critical"
        assert out["errors"] == ["compliance: LLM down"]

    async def test_kpi_node_success_and_error(self, monkeypatch):
        async def ok(raw):
            return _FakeResult({"snapshots": []})

        monkeypatch.setattr(graph_state._kpi, "run", ok)
        assert (await graph_state.kpi_analytics_node(_state()))["kpi_analytics"] == {
            "snapshots": []
        }

        async def bad(raw):
            return _FakeResult(error="platform down")

        monkeypatch.setattr(graph_state._kpi, "run", bad)
        out = await graph_state.kpi_analytics_node(_state())
        assert out["errors"] == ["kpi_analytics: platform down"]

    async def test_roi_and_optimization_nodes(self, monkeypatch):
        async def roi_ok(raw):
            return _FakeResult({"blended_roas": 3.0})

        async def opt_ok(raw):
            return _FakeResult({"suggestions": []})

        monkeypatch.setattr(graph_state._roi, "run", roi_ok)
        monkeypatch.setattr(graph_state._optimization, "run", opt_ok)
        assert (await graph_state.roi_calculator_node(_state()))["roi_calculator"] == {
            "blended_roas": 3.0
        }
        assert (await graph_state.optimization_node(_state()))["optimization"] == {
            "suggestions": []
        }

        async def bad(raw):
            return _FakeResult(error="down")

        monkeypatch.setattr(graph_state._roi, "run", bad)
        monkeypatch.setattr(graph_state._optimization, "run", bad)
        assert (await graph_state.roi_calculator_node(_state()))["errors"] == [
            "roi_calculator: down"
        ]
        assert (await graph_state.optimization_node(_state()))["errors"] == ["optimization: down"]


class TestHumanReviewNode:
    async def test_auto_blocks_critical_compliance(self):
        out = await graph_state.human_review_node(
            _state(
                compliance_verdict={"approved": False, "risk_level": "critical"},
                creative={},
                media_plan={},
            )
        )
        assert out["review_status"] == "auto_blocked"
        assert "Auto-blocked" in out["reviewer_comments"]

    async def test_skips_when_approval_not_required(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL", "false")
        get_settings.cache_clear()
        out = await graph_state.human_review_node(
            _state(compliance_verdict={"approved": True, "risk_level": "low"})
        )
        assert out == {"review_status": "skipped"}

    async def test_approve_issues_token(self, monkeypatch):
        monkeypatch.setattr(
            graph_state,
            "interrupt",
            lambda packet: {
                "approved": True,
                "reviewer_email": "rev@example.com",
                "comments": "ship it",
            },
        )
        state = _state(compliance_verdict={"approved": True, "risk_level": "low"})
        out = await graph_state.human_review_node(state)
        assert out["review_status"] == "approved"
        assert out["reviewer_email"] == "rev@example.com"
        from adforge.mcp_servers import permissions

        claims = permissions._verify_approval_claims(
            out["approval_token"],
            state["campaign_id"],
            "publish_campaign",
        )
        assert claims is not None
        assert claims["reviewer_email"] == "rev@example.com"

    async def test_reject_increments_revision_and_passes_comments(self, monkeypatch):
        monkeypatch.setattr(
            graph_state,
            "interrupt",
            lambda packet: {"approved": False, "comments": "too salesy"},
        )
        out = await graph_state.human_review_node(
            _state(
                compliance_verdict={"approved": True, "risk_level": "low"},
                revision_count=1,
            )
        )
        assert out["review_status"] == "rejected"
        assert out["revision_count"] == 2
        assert out["reviewer_comments"] == "too salesy"

    async def test_reject_without_comment_uses_default(self, monkeypatch):
        monkeypatch.setattr(graph_state, "interrupt", lambda packet: {"approved": False})
        out = await graph_state.human_review_node(
            _state(compliance_verdict={"approved": True, "risk_level": "low"})
        )
        assert out["reviewer_comments"] == "No comment provided."


class TestCheckpointer:
    async def test_none_backend_compiles_without_checkpointer(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("CHECKPOINTER_BACKEND", "none")
        get_settings.cache_clear()
        graph_state.reset_compiled_graph()
        graph = await graph_state.get_compiled_graph()
        assert graph.checkpointer is None

    async def test_memory_backend_returns_memory_saver(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
        get_settings.cache_clear()
        graph_state.reset_compiled_graph()
        graph = await graph_state.get_compiled_graph()
        assert isinstance(graph.checkpointer, MemorySaver)

    async def test_postgres_failure_falls_back_to_memory(self, monkeypatch):
        import psycopg_pool

        from adforge.config import get_settings

        def broken_pool(*args, **kwargs):
            raise RuntimeError("db offline")

        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        get_settings.cache_clear()
        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", broken_pool)
        checkpointer = await graph_state._build_checkpointer()
        assert isinstance(checkpointer, MemorySaver)

    def test_checkpointer_conninfo_prefers_ipv4_loopback(self):
        assert (
            graph_state._checkpointer_conninfo("postgresql+asyncpg://u:p@localhost:5432/db")
            == "postgresql://u:p@127.0.0.1:5432/db"
        )
        assert (
            graph_state._checkpointer_conninfo("postgresql+asyncpg://u:p@db.internal:5432/db")
            == "postgresql://u:p@db.internal:5432/db"
        )

    async def test_postgres_backend_builds_durable_saver(self, monkeypatch):
        import psycopg_pool
        from langgraph.checkpoint.postgres import aio as pg_aio

        from adforge.config import get_settings

        captured: dict = {}

        class FakePool:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def open(self, **kwargs):
                captured["opened"] = kwargs

            async def close(self):
                captured["closed"] = True

        class FakeSaver:
            def __init__(self, pool):
                self.pool = pool

            async def setup(self):
                captured["setup"] = True

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        get_settings.cache_clear()
        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
        monkeypatch.setattr(pg_aio, "AsyncPostgresSaver", FakeSaver)

        checkpointer = await graph_state._build_checkpointer()
        assert isinstance(checkpointer, FakeSaver)
        assert captured["conninfo"] == "postgresql://u:p@127.0.0.1:5432/db"
        assert captured["opened"] == {"wait": True, "timeout": 10}
        assert captured["setup"] is True
        assert "closed" not in captured

    async def test_postgres_setup_failure_closes_pool(self, monkeypatch):
        import psycopg_pool
        from langgraph.checkpoint.postgres import aio as pg_aio

        from adforge.config import get_settings

        captured: dict = {}

        class FakePool:
            def __init__(self, **kwargs):
                pass

            async def open(self, **kwargs):
                pass

            async def close(self):
                captured["closed"] = True

        class FailingSaver:
            def __init__(self, pool):
                pass

            async def setup(self):
                raise RuntimeError("schema setup failed")

        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        get_settings.cache_clear()
        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
        monkeypatch.setattr(pg_aio, "AsyncPostgresSaver", FailingSaver)

        checkpointer = await graph_state._build_checkpointer()
        assert isinstance(checkpointer, MemorySaver)
        assert captured["closed"] is True

    async def test_compiled_graph_is_cached(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
        get_settings.cache_clear()
        graph_state.reset_compiled_graph()
        first = await graph_state.get_compiled_graph()
        second = await graph_state.get_compiled_graph()
        assert first is second


class TestGraphEndToEndUnit:
    @pytest.fixture(autouse=True)
    def _deterministic_kpi(self, monkeypatch):
        """Replace the real ad-platform HTTP fetch with deterministic rows.

        Unit tests have no ad-platform credentials or external campaign ids;
        the real fetcher would correctly fail. Persistence is a no-op so the
        graph can be asserted end-to-end without a database.
        """
        import adforge.agents.kpi_analytics.agent as kpi_agent_module
        from adforge.agents.kpi_analytics.fetcher import build_snapshot

        async def fake_fetch(scope, channel, campaign_id, **kwargs):
            return [
                build_snapshot(
                    channel=channel,
                    campaign_id=campaign_id,
                    campaign_external_id=kwargs.get("external_id") or f"{channel}-1",
                    platform_date=date(2026, 1, day),
                    impressions=1_000,
                    clicks=50,
                    spend_usd=100.0,
                    conversions=5,
                    revenue_usd=300.0,
                    source=f"{channel}_ads",
                )
                for day in range(1, 8)
            ]

        async def fake_persist(scope, campaign_id, models):
            return len(models)

        monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", fake_fetch)
        monkeypatch.setattr(kpi_agent_module, "persist_snapshots", fake_persist)

    async def test_full_graph_skips_hitl_when_not_required(self, graph_llm, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL", "false")
        get_settings.cache_clear()

        graph_state.reset_compiled_graph()
        graph = graph_state.build_graph().compile(checkpointer=MemorySaver())
        campaign_id = uuid4()
        final = await graph.ainvoke(
            {
                "request_id": str(uuid4()),
                "tenant_id": "tenant_a",
                "campaign_id": str(campaign_id),
                "brief": CampaignBrief(
                    product_name="Acme",
                    product_description="A project management tool for dev teams.",
                    target_audience="Engineering managers",
                    budget_usd=25000.0,
                    duration_days=14,
                ).model_dump(),
                "errors": [],
            },
            config={"configurable": {"thread_id": graph_state.thread_id_for(campaign_id)}},
        )
        assert final["review_status"] == "skipped"
        assert final["optimization"]["suggestions"]
        assert final["roi_calculator"]["blended_roas"] == 3.0
        assert len(final["kpi_analytics"]["snapshots"]) == 14  # 7 days × 2 channels
        assert final["errors"] == []

    async def test_hitl_pause_then_approve(self, graph_llm):
        graph_state.reset_compiled_graph()
        graph = graph_state.build_graph().compile(checkpointer=MemorySaver())
        campaign_id = uuid4()
        config = {"configurable": {"thread_id": graph_state.thread_id_for(campaign_id)}}

        paused = await graph.ainvoke(
            {
                "request_id": str(uuid4()),
                "tenant_id": "tenant_a",
                "campaign_id": str(campaign_id),
                "brief": CampaignBrief(
                    product_name="Acme",
                    product_description="A project management tool for dev teams.",
                    target_audience="Engineering managers",
                    budget_usd=25000.0,
                    duration_days=7,
                ).model_dump(),
                "errors": [],
            },
            config=config,
        )
        snapshot = await graph.aget_state(config)
        assert "human_review" in (snapshot.next or ())
        assert "__interrupt__" in paused

        final = await graph.ainvoke(
            Command(
                resume={
                    "approved": True,
                    "reviewer_email": "rev@example.com",
                    "comments": "looks good",
                }
            ),
            config=config,
        )
        assert final["review_status"] == "approved"
        from adforge.mcp_servers import permissions

        claims = permissions._verify_approval_claims(
            final["approval_token"], str(campaign_id), "publish_campaign"
        )
        assert claims is not None
        assert claims["reviewer_email"] == "rev@example.com"
        assert final["reviewer_email"] == "rev@example.com"
        assert final["optimization"]["suggestions"]

    async def test_hitl_reject_loops_back_to_creative(self, graph_llm):
        graph_state.reset_compiled_graph()
        graph = graph_state.build_graph().compile(checkpointer=MemorySaver())
        campaign_id = uuid4()
        config = {"configurable": {"thread_id": graph_state.thread_id_for(campaign_id)}}

        await graph.ainvoke(
            {
                "request_id": str(uuid4()),
                "tenant_id": "tenant_a",
                "campaign_id": str(campaign_id),
                "brief": CampaignBrief(
                    product_name="Acme",
                    product_description="A project management tool for dev teams.",
                    target_audience="Engineering managers",
                    budget_usd=25000.0,
                    duration_days=7,
                ).model_dump(),
                "errors": [],
            },
            config=config,
        )
        final = await graph.ainvoke(
            Command(resume={"approved": False, "comments": "too salesy"}),
            config=config,
        )
        snapshot = await graph.aget_state(config)
        # Rejection routes back through creative → compliance → review.
        assert "human_review" in (snapshot.next or ())
        assert final["review_status"] == "rejected"
        assert final["revision_count"] == 1
        # The reviewer's feedback was fed into the creative revision prompt.
        assert any(
            "too salesy" in str(message.get("content", ""))
            for call in graph_llm.calls
            for message in call
        )
