"""Tests for the FastAPI app + LangGraph orchestrator.

Integration tests require Postgres + Redis — marked separately so unit-test
runs work without infrastructure.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from adforge.api.main import app
    return TestClient(app)


class TestApi:
    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_root_endpoint_lists_agents(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert "research" in data["agents"]
        assert "roi_calculator" in data["agents"]
        assert "compliance" in data["agents"]

    def test_campaigns_require_tenant_header(self, client):
        r = client.get("/campaigns")
        assert r.status_code == 400

    def test_campaigns_with_tenant_header_returns_list(self, client):
        r = client.get("/campaigns", headers={"X-AdForge-Tenant": "test"})
        # 200 even if empty
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestGraph:
    def test_graph_builds_without_error(self):
        from adforge.graph.state import build_graph
        builder = build_graph()
        # Just verify all nodes were added.
        nodes = builder.nodes
        expected = {"research", "creative_strategist", "media_planner",
                    "compliance", "kpi_analytics", "roi_calculator", "optimization"}
        assert expected.issubset(set(nodes.keys()))

    def test_route_after_compliance_approved(self):
        from adforge.graph.state import route_after_compliance
        state = {"compliance_verdict": {"approved": True}}
        assert route_after_compliance(state) == "kpi_analytics"

    def test_route_after_compliance_rejected(self):
        from adforge.graph.state import route_after_compliance
        state = {"compliance_verdict": {"approved": False, "risk_level": "critical"}}
        # In v1 we still route forward (no revision loop)
        assert route_after_compliance(state) == "kpi_analytics"
