"""Integration tests for graph execution edge cases against a real database.

The happy path is covered in ``test_api_integration.py``. These tests exercise
the failure paths that keep a broken agent from taking the whole campaign down
and the metadata endpoints operators use for health checks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _brief(name: str = "Integration Failure Test") -> dict:
    return {
        "product_name": name,
        "product_description": "A lightweight CRM for B2B sales teams.",
        "target_audience": "Sales managers at B2B SaaS companies.",
        "budget_usd": 8000.0,
        "duration_days": 7,
        "business_objective": "conversion",
        "channels_hint": ["google", "linkedin"],
    }


async def _create_campaign(api_client, headers, name="Integration Failure Test"):
    response = await api_client.post(
        "/campaigns", json={"name": name, "brief": _brief(name)}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestApiMetadata:
    async def test_health_and_root_describe_the_running_service(self, api_client):
        health = await api_client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        root = await api_client.get("/")
        assert root.status_code == 200
        body = root.json()
        assert body["docs"] == "/docs"
        assert {"research", "compliance", "optimization"}.issubset(set(body["agents"]))

    async def test_run_requires_tenant_and_valid_campaign_header(self, api_client, tenant_headers):
        missing_tenant = await api_client.post(f"/campaigns/{uuid4()}/run")
        assert missing_tenant.status_code == 400
        assert "X-AdForge-Tenant" in missing_tenant.json()["detail"]

        bad_campaign = await api_client.post(
            f"/campaigns/{uuid4()}/run",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Campaign": "not-a-uuid",
            },
        )
        assert bad_campaign.status_code == 400
        assert "valid UUID" in bad_campaign.json()["detail"]


class TestGraphFailurePath:
    async def test_agent_failure_marks_campaign_failed_without_crashing_graph(
        self, api_client, tenant_headers, graph_llm, monkeypatch
    ):
        """A dead agent must surface as an error, not a 500 or a fake approval."""
        from support import FailingLLM

        import adforge.graph.state as graph_state
        from adforge.config import get_settings
        from adforge.guardrails.exceptions import GuardrailError

        # Dev-mode: no human review, so the graph runs to completion and the
        # persisted error can be observed as a terminal campaign status.
        monkeypatch.setenv("GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL", "false")
        get_settings.cache_clear()
        monkeypatch.setattr(
            graph_state._research,
            "_llm",
            FailingLLM(GuardrailError("research provider unavailable")),
        )

        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)

        run = await api_client.post(f"/campaigns/{campaign['id']}/run", headers=headers)
        assert run.status_code == 200, run.text
        body = run.json()
        assert body["status"] == "failed"
        assert body["paused_for_review"] is False
        assert body["final_state"]["research"] == {}
        assert any(error.startswith("research:") for error in body["final_state"]["errors"])
        # Downstream agents still ran with the (empty) research payload.
        assert body["final_state"]["creative"]["headline"]
        assert body["final_state"]["optimization"]["suggestions"]

        fetched = await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "failed"

        # The failed run is still visible in the trace API.
        traces = await api_client.get(f"/traces/{campaign['id']}/runs", headers=headers)
        assert traces.status_code == 200
        names = {row["agent_name"] for row in traces.json()["runs"]}
        assert "research" in names
