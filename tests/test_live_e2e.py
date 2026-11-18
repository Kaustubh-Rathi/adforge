"""Opt-in live end-to-end test: real LLM + embedded Postgres + real API.

This is the highest-fidelity verification available without paid ad accounts:
a campaign is created through the API, the full agent graph runs against the
configured LLM endpoint, the graph pauses for HITL, and the review approval
resumes it to completion.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = [pytest.mark.live, pytest.mark.integration]

LIVE_ENABLED = os.getenv("ADFORGE_LIVE_TESTS") == "1"


def _brief() -> dict:
    return {
        "product_name": "Acme CRM",
        "product_description": (
            "Lightweight CRM for B2B sales teams with Slack-native follow-ups."
        ),
        "target_audience": "Sales managers at 20-200 person B2B SaaS companies.",
        "budget_usd": 12000.0,
        "duration_days": 7,
        "business_objective": "conversion",
        "channels_hint": ["google", "linkedin"],
        "constraints": ["No comparison-brand bidding"],
    }


@pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="set ADFORGE_LIVE_TESTS=1 to run live-service tests",
)
async def test_live_campaign_runs_through_hitl(db_env, monkeypatch):
    from adforge.api.main import app
    from adforge.config import get_settings
    from adforge.graph import state as graph_state
    from adforge.llm.factory import get_llm

    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_model):
        pytest.skip("LLM_BASE_URL / LLM_MODEL are not configured")

    # Use the real configured provider, not the test fixture's scripted one.
    monkeypatch.setenv("LLM_PROVIDER", "opencode")
    monkeypatch.setenv("APP_REVIEWER_ALLOWLIST", "")
    get_settings.cache_clear()
    get_llm.cache_clear()
    for agent in (
        graph_state._research,
        graph_state._creative,
        graph_state._media_planner,
        graph_state._compliance,
        graph_state._kpi,
        graph_state._roi,
        graph_state._optimization,
    ):
        agent._llm = None
    graph_state.reset_compiled_graph()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://adforge.test", timeout=600.0
    ) as client:
        headers = {"X-AdForge-Tenant": "live-e2e"}
        created = await client.post(
            "/campaigns", json={"name": "Live E2E", "brief": _brief()}, headers=headers
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]

        run = await client.post(f"/campaigns/{campaign_id}/run", headers=headers)
        assert run.status_code == 200, run.text
        body = run.json()
        state = body["final_state"]

        # Free-tier quota is shared and can be exhausted by other users. Treat
        # that as an environmental skip, not a product failure, just like the
        # live health check.
        if any("HTTP 429" in error for error in state.get("errors", [])):
            pytest.skip("opencode free-tier quota is exhausted; rerun after reset")

        # The real LLM must have produced schema-valid payloads, not fallbacks.
        assert state["research"], state.get("errors")
        assert state["creative"].get("headline"), state.get("errors")
        assert state["media_plan"].get("channel_allocations"), state.get("errors")
        assert state["compliance_verdict"].get("approved") is not None
        # The approval token is a bearer credential and must never be exposed.
        assert "approval_token" not in state

        if body["status"] != "reviewing":
            verdict = state.get("compliance_verdict", {})
            pytest.fail(
                f"live graph did not pause for review: status={body['status']} "
                f"risk={verdict.get('risk_level')} "
                f"approved={verdict.get('approved')} "
                f"blocked_terms={verdict.get('blocked_terms')} "
                f"flagged={verdict.get('flagged_issues')} "
                f"targeting={state.get('media_plan', {}).get('audience_targeting')} "
                f"errors={state.get('errors')}"
            )

        approve = await client.post(
            f"/reviews/{campaign_id}/approve",
            json={"approved": True, "comments": "live e2e approval"},
            headers={**headers, "X-AdForge-Reviewer": "live@adforge.test"},
        )
        assert approve.status_code == 200, approve.text
        approved = approve.json()
        assert approved["decision"] == "approved"
        assert approved["status"] == "approved", approved
        assert approved["approval_token_issued"] is True
        assert "approval_token" not in approved

        # Approval authorises publishing; it does not spend money. Without a
        # configured publishing adapter the campaign must stay approved.
        fetched = await client.get(f"/campaigns/{campaign_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "approved"
