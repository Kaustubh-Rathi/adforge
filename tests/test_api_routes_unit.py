"""Unit tests for the FastAPI route logic with a fake database.

Integration tests cover the same routes against real PostgreSQL. These tests
pin down response shaping and error handling without a database so the unit
tier can cover the HTTP contract cheaply.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from adforge.api.routes import campaigns, kpis, reviews, traces
from adforge.guardrails.contracts import CampaignBrief


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        return None


def _brief() -> CampaignBrief:
    return CampaignBrief(
        product_name="Acme CRM",
        product_description="A lightweight CRM for B2B sales teams.",
        target_audience="Sales managers at B2B SaaS companies.",
        budget_usd=5000.0,
        duration_days=7,
    )


def _campaign_row(**overrides):
    base = {
        "id": uuid4(),
        "tenant_id": "acme",
        "name": "Acme Launch",
        "brief": {"product_name": "Acme CRM"},
        "status": "draft",
        "total_spend_usd": 12.5,
        "total_revenue_usd": 40.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCampaignRoutes:
    async def test_create_campaign_inserts_with_tenant_scope(self):
        db = _FakeDb()
        body = campaigns.CreateCampaignRequest(name="Acme Launch", brief=_brief())

        response = await campaigns.create_campaign(body, tenant_id="acme", db=db)

        assert response.status == "draft"
        assert response.tenant_id == "acme"
        assert response.brief["product_name"] == "Acme CRM"
        statement, params = db.calls[0]
        assert "INSERT INTO campaigns" in statement
        assert params["tid"] == "acme"

    async def test_list_campaigns_parses_dict_and_json_briefs(self):
        rows = [
            _campaign_row(),
            _campaign_row(brief=json.dumps({"product_name": "JSON CRM"})),
        ]

        result = await campaigns.list_campaigns(tenant_id="acme", db=_FakeDb(_Result(rows)))

        assert [c.name for c in result] == ["Acme Launch", "Acme Launch"]
        assert result[1].brief["product_name"] == "JSON CRM"

    async def test_get_campaign_returns_row_or_404(self):
        row = _campaign_row()
        found = await campaigns.get_campaign(row.id, tenant_id="acme", db=_FakeDb(_Result([row])))
        assert found.id == row.id
        assert found.total_spend_usd == 12.5

        with pytest.raises(HTTPException) as exc:
            await campaigns.get_campaign(uuid4(), tenant_id="acme", db=_FakeDb(_Result()))
        assert exc.value.status_code == 404


class TestKpiRoutes:
    def _row(self):
        return SimpleNamespace(
            id=uuid4(),
            channel="google",
            campaign_external_id="ext-1",
            impressions=1000,
            clicks=100,
            spend_usd=50.0,
            conversions=10,
            revenue_usd=200.0,
            ctr=0.1,
            cvr=0.1,
            cpc=0.5,
            cpa=5.0,
            cpm=50.0,
            roas=4.0,
            is_mock=False,
            captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        )

    async def test_list_kpis_with_and_without_channel_filter(self):
        campaign_id = uuid4()
        all_rows = await kpis.list_kpis(
            campaign_id,
            days_back=7,
            channel=None,
            tenant_id="acme",
            db=_FakeDb(_Result([self._row()])),
        )
        assert all_rows["count"] == 1
        assert all_rows["snapshots"][0]["captured_at"].startswith("2026-11-01")
        assert all_rows["snapshots"][0]["is_mock"] is False

        filtered = await kpis.list_kpis(
            campaign_id,
            days_back=3,
            channel="google",
            tenant_id="acme",
            db=_FakeDb(_Result([self._row()])),
        )
        assert filtered["snapshots"][0]["channel"] == "google"

    async def test_list_kpis_query_selects_the_mock_flag(self):
        """Provenance has to reach the API, or the demo cannot prove it is mock."""
        db = _FakeDb(_Result([self._row()]))
        await kpis.list_kpis(uuid4(), days_back=7, channel=None, tenant_id="acme", db=db)
        statement, _ = db.calls[0]
        assert "is_mock" in statement

    async def test_list_kpis_discloses_mock_rows(self):
        mock_row = self._row()
        mock_row.is_mock = True
        result = await kpis.list_kpis(
            uuid4(),
            days_back=7,
            channel=None,
            tenant_id="acme",
            db=_FakeDb(_Result([mock_row])),
        )
        assert result["snapshots"][0]["is_mock"] is True

    async def test_aggregate_kpis_computes_ratios_and_handles_dates(self):
        rows = [
            SimpleNamespace(
                bucket=date(2026, 11, 1),
                impressions=1000,
                clicks=100,
                spend_usd=50.0,
                conversions=10,
                revenue_usd=200.0,
                any_mock=False,
                all_mock=False,
            ),
            SimpleNamespace(
                bucket=None,
                impressions=0,
                clicks=0,
                spend_usd=0.0,
                conversions=0,
                revenue_usd=0.0,
                any_mock=None,
                all_mock=None,
            ),
        ]

        by_day = await kpis.aggregate_kpis(
            uuid4(), group_by="day", tenant_id="acme", db=_FakeDb(_Result(rows))
        )

        first, empty = by_day["buckets"]
        assert first["bucket"] == "2026-11-01"
        assert first["roas"] == 4.0
        assert first["cpa"] == 5.0
        assert first["ctr"] == 0.1
        assert first["is_mock"] is False
        assert first["all_simulated"] is False
        assert empty["roas"] == 0.0
        assert empty["cpa"] == 0.0
        assert empty["ctr"] == 0.0

    async def test_aggregate_flags_a_partly_mocked_bucket(self):
        """A bucket mixing measured and simulated rows must not read as real."""
        rows = [
            SimpleNamespace(
                bucket="google",
                impressions=1000,
                clicks=100,
                spend_usd=50.0,
                conversions=10,
                revenue_usd=200.0,
                any_mock=True,
                all_mock=False,
            ),
        ]
        result = await kpis.aggregate_kpis(
            uuid4(), group_by="channel", tenant_id="acme", db=_FakeDb(_Result(rows))
        )
        bucket = result["buckets"][0]
        assert bucket["is_mock"] is True
        assert bucket["all_simulated"] is False


class TestTraceRoutes:
    def _run_row(self):
        started = datetime(2026, 11, 1, tzinfo=UTC)
        return SimpleNamespace(
            id=uuid4(),
            request_id=uuid4(),
            agent_name="research",
            input_payload={"agent": "research"},
            output_payload=json.dumps({"ok": True}),
            usage={"total_tokens": 10},
            error=None,
            status="succeeded",
            started_at=started,
            finished_at=started,
        )

    async def test_list_agent_runs_supports_filter_and_duration(self):
        campaign_id = uuid4()
        result = await traces.list_agent_runs(
            campaign_id,
            agent_name="research",
            limit=10,
            tenant_id="acme",
            db=_FakeDb(_Result([self._run_row()])),
        )
        assert result["count"] == 1
        assert result["runs"][0]["output_payload"] == {"ok": True}
        assert result["runs"][0]["duration_seconds"] == 0.0

        unfiltered = await traces.list_agent_runs(
            campaign_id,
            agent_name=None,
            limit=10,
            tenant_id="acme",
            db=_FakeDb(_Result([self._run_row()])),
        )
        assert unfiltered["runs"][0]["agent_name"] == "research"

    async def test_list_audit_events_supports_filter(self):
        row = SimpleNamespace(
            event_id=uuid4(),
            event_type="review.decision",
            agent_name="human_review",
            request_id=None,
            payload=json.dumps({"decision": "approved"}),
            occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        result = await traces.list_audit_events(
            uuid4(),
            event_type="review.decision",
            limit=10,
            tenant_id="acme",
            db=_FakeDb(_Result([row])),
        )
        assert result["events"][0]["payload"] == {"decision": "approved"}
        assert result["events"][0]["request_id"] is None


class _FakeGraph:
    def __init__(self, state, next_nodes=(), error=None, parked=True):
        self.state = state
        self.next = tuple(next_nodes)
        self.error = error
        self.parked = parked
        self.invoked = False

    async def ainvoke(self, command, config):
        self.invoked = True
        if self.error:
            raise self.error
        return self.state

    async def aget_state(self, config):
        # Before the resume the campaign is parked at the gate; afterwards the
        # snapshot reflects whatever the resumed graph produced.
        if not self.invoked:
            return SimpleNamespace(next=("human_review",) if self.parked else ())
        return SimpleNamespace(next=self.next)


class TestReviewHelpers:
    async def test_resume_graph_derives_each_terminal_status(self, monkeypatch):
        campaign_id = uuid4()

        async def fake_graph_for(graph):
            async def _get():
                return graph

            return _get

        cases = [
            (
                _FakeGraph({"review_status": "approved"}, next_nodes=["human_review"]),
                True,
                "reviewing",
            ),
            (_FakeGraph({"review_status": "auto_blocked"}), True, "blocked"),
            (
                _FakeGraph({"review_status": "rejected", "revision_count": 3}),
                False,
                "blocked",
            ),
            # Post-approval analytics failures do not revoke human approval.
            (
                _FakeGraph({"review_status": "approved", "errors": ["kpi boom"]}),
                True,
                "approved",
            ),
            (_FakeGraph({"review_status": "approved"}), True, "approved"),
            (
                _FakeGraph({"review_status": "pending", "errors": ["boom"]}),
                False,
                "failed",
            ),
            (_FakeGraph({"review_status": "pending"}), False, "reviewing"),
        ]
        for graph, approved, expected in cases:
            monkeypatch.setattr(reviews, "get_compiled_graph", await fake_graph_for(graph))
            _, status = await reviews._resume_graph(campaign_id, "rev@acme.test", "ok", approved)
            assert status == expected

    async def test_resume_graph_wraps_failures_as_500(self, monkeypatch):
        async def broken_graph():
            return _FakeGraph({}, error=RuntimeError("checkpoint offline"))

        monkeypatch.setattr(reviews, "get_compiled_graph", broken_graph)
        with pytest.raises(HTTPException) as exc:
            await reviews._resume_graph(uuid4(), None, "", False)
        assert exc.value.status_code == 500
        assert "checkpoint offline" in exc.value.detail

    async def test_resume_refuses_when_no_checkpoint_is_parked(self, monkeypatch):
        """A lost checkpoint must not silently re-run the graph."""

        async def no_gate():
            return _FakeGraph({}, parked=False)

        monkeypatch.setattr(reviews, "get_compiled_graph", no_gate)
        with pytest.raises(HTTPException) as exc:
            await reviews._resume_graph(uuid4(), "rev@acme.test", "", True)
        assert exc.value.status_code == 409
        assert "no paused human-review checkpoint" in exc.value.detail

    async def test_reviewer_allowlist_blocks_unknown_reviewers(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("APP_REVIEWER_ALLOWLIST", "rev@acme.test, lead@acme.test")
        get_settings.cache_clear()
        try:
            reviews._assert_reviewer_allowed("REV@acme.test")
            with pytest.raises(HTTPException) as exc:
                reviews._assert_reviewer_allowed("intruder@example.com")
            assert exc.value.status_code == 403
            with pytest.raises(HTTPException):
                reviews._assert_reviewer_allowed(None)
        finally:
            get_settings.cache_clear()

    async def test_campaign_ownership_and_status_updates_use_tenant_scope(self):
        campaign_id = uuid4()
        owned = _FakeDb(_Result([SimpleNamespace()]))
        await reviews._assert_campaign_owned(owned, campaign_id, "acme")
        assert owned.calls[0][1]["tid"] == "acme"

        missing = _FakeDb(_Result())
        with pytest.raises(HTTPException) as exc:
            await reviews._assert_campaign_owned(missing, campaign_id, "acme")
        assert exc.value.status_code == 404

        live_db = _FakeDb()
        await reviews._update_campaign_status(live_db, campaign_id, "acme", "live")
        assert "started_at" in live_db.calls[0][0]
        assert live_db.calls[0][1]["status"] == "live"

        paused_db = _FakeDb()
        await reviews._update_campaign_status(paused_db, campaign_id, "acme", "reviewing")
        assert "started_at" not in paused_db.calls[0][0]
        assert paused_db.calls[0][1]["status"] == "reviewing"

        audit_db = _FakeDb()
        await reviews._record_review_event(
            audit_db, campaign_id, "acme", "rev@acme.test", "approved", "ship it"
        )
        _, params = audit_db.calls[0]
        assert params["tid"] == "acme"
        assert json.loads(params["p"])["decision"] == "approved"

    async def test_list_pending_reviews_shapes_queue(self):
        row = SimpleNamespace(
            id=uuid4(),
            name="Acme Launch",
            brief=json.dumps({"product_name": "Acme CRM"}),
            status="reviewing",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        result = await reviews.list_pending_reviews(tenant_id="acme", db=_FakeDb(_Result([row])))
        assert result["count"] == 1
        assert result["pending"][0]["brief"]["product_name"] == "Acme CRM"

    async def test_review_packet_uses_latest_successful_agent_outputs(self):
        campaign_id = uuid4()
        campaign = SimpleNamespace(
            id=campaign_id,
            name="Acme Launch",
            status="reviewing",
            brief={"product_name": "Acme CRM"},
        )
        runs = [
            SimpleNamespace(
                agent_name="creative_strategist",
                output_payload={"headline": "Ship faster"},
            ),
            SimpleNamespace(
                agent_name="media_planner",
                output_payload=json.dumps({"total_budget_usd": 5000.0}),
            ),
            SimpleNamespace(agent_name="unrelated", output_payload={"ignored": True}),
        ]
        result = await reviews.get_review_packet(
            campaign_id, tenant_id="acme", db=_FakeDb(_Result([campaign]), _Result(runs))
        )
        assert result["campaign"]["name"] == "Acme Launch"
        assert result["packet"]["creative"] == {"headline": "Ship faster"}
        assert result["packet"]["media_plan"]["total_budget_usd"] == 5000.0
        assert result["packet"]["compliance_verdict"] == {}

        with pytest.raises(HTTPException) as exc:
            await reviews.get_review_packet(campaign_id, tenant_id="acme", db=_FakeDb(_Result()))
        assert exc.value.status_code == 404
