"""Integration tests — embedded PostgreSQL + the real FastAPI app.

These cover the full campaign lifecycle: create → run graph (pauses for HITL)
→ inspect review packet → approve / reject → KPIs, traces, and audit events.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

pytestmark = pytest.mark.integration


def _brief(name: str = "Acme CRM Launch") -> dict:
    return {
        "product_name": name,
        "product_description": (
            "Lightweight CRM for B2B sales teams with Slack-native follow-ups."
        ),
        "target_audience": "Sales managers at 20-200 person B2B SaaS companies.",
        "budget_usd": 25000.0,
        "duration_days": 7,
        "business_objective": "conversion",
        "channels_hint": ["google", "linkedin"],
        "constraints": ["No comparison-brand bidding"],
    }


async def _create_campaign(api_client, headers, name="Acme CRM Launch"):
    response = await api_client.post(
        "/campaigns", json={"name": name, "brief": _brief(name)}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run_campaign(api_client, headers, campaign_id):
    response = await api_client.post(f"/campaigns/{campaign_id}/run", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


class TestCampaignCrud:
    async def test_create_list_get_and_tenant_isolation(self, api_client, tenant_headers):
        acme = tenant_headers("acme")
        other = tenant_headers("other")

        created = await _create_campaign(api_client, acme)
        assert created["status"] == "draft"
        assert created["tenant_id"] == "acme"
        assert created["brief"]["product_name"] == "Acme CRM Launch"

        listed = (await api_client.get("/campaigns", headers=acme)).json()
        assert any(c["id"] == created["id"] for c in listed)
        assert all(c["tenant_id"] == "acme" for c in listed)

        fetched = await api_client.get(f"/campaigns/{created['id']}", headers=acme)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == created["id"]

        # Tenant isolation: another tenant cannot see or fetch the campaign.
        other_list = (await api_client.get("/campaigns", headers=other)).json()
        assert other_list == []
        hidden = await api_client.get(f"/campaigns/{created['id']}", headers=other)
        assert hidden.status_code == 404

    async def test_invalid_brief_is_rejected(self, api_client, tenant_headers):
        bad = _brief()
        bad["budget_usd"] = 0
        response = await api_client.post(
            "/campaigns",
            json={"name": "Bad", "brief": bad},
            headers=tenant_headers("acme"),
        )
        assert response.status_code == 422

    async def test_run_unknown_campaign_returns_404(self, api_client, tenant_headers):
        response = await api_client.post(
            f"/campaigns/{uuid4()}/run", headers=tenant_headers("acme")
        )
        assert response.status_code == 404


class TestCampaignReviewLifecycle:
    async def test_run_pauses_and_approve_authorises_publish(
        self, api_client, tenant_headers, graph_llm
    ):
        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)

        run = await _run_campaign(api_client, headers, campaign["id"])
        assert run["status"] == "reviewing"
        assert run["paused_for_review"] is True
        # The graph is suspended *inside* human_review, so its update hasn't
        # been committed to state yet; the interrupt itself marks the pause.
        assert run["final_state"].get("review_status") in (None, "pending")
        assert run["final_state"]["creative"]["headline"]
        assert run["final_state"]["media_plan"]["total_budget_usd"] == 25000.0
        assert "approval_token" not in run["final_state"]

        pending = (await api_client.get("/reviews/pending", headers=headers)).json()
        assert pending["count"] == 1
        assert pending["pending"][0]["id"] == campaign["id"]

        packet_response = await api_client.get(f"/reviews/{campaign['id']}/packet", headers=headers)
        assert packet_response.status_code == 200
        packet = packet_response.json()["packet"]
        assert packet["creative"]["headline"] == run["final_state"]["creative"]["headline"]
        assert packet["media_plan"]["channel_allocations"]
        assert packet["compliance_verdict"]["approved"] is True

        approve = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "looks good"},
            headers={**headers, "X-AdForge-Reviewer": "rev@acme.test"},
        )
        assert approve.status_code == 200, approve.text
        body = approve.json()
        assert body["decision"] == "approved"
        # Approval authorises publishing; it must not launch or spend money.
        assert body["status"] == "approved"
        assert body["approval_token_issued"] is True
        assert "approval_token" not in body
        assert "publish" in body["next_step"]

        # Campaign status is approved, not live, until the publish adapter runs.
        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["status"] == "approved"

        # KPI snapshots exist for both channels, 7 days each.
        kpis = (await api_client.get(f"/kpis/{campaign['id']}/snapshots", headers=headers)).json()
        assert kpis["count"] == 14
        assert {row["channel"] for row in kpis["snapshots"]} == {"google", "linkedin"}

        by_channel = (
            await api_client.get(
                f"/kpis/{campaign['id']}/aggregate?group_by=channel", headers=headers
            )
        ).json()
        assert {bucket["bucket"] for bucket in by_channel["buckets"]} == {
            "google",
            "linkedin",
        }
        by_day = (
            await api_client.get(f"/kpis/{campaign['id']}/aggregate?group_by=day", headers=headers)
        ).json()
        assert len(by_day["buckets"]) == 7

        # Campaign totals were rolled up from the KPI snapshots.
        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["total_spend_usd"] > 0
        assert fetched["total_revenue_usd"] > 0

    async def test_approve_requires_true_and_reviewing_status(
        self, api_client, tenant_headers, graph_llm
    ):
        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])
        review_headers = {**headers, "X-AdForge-Reviewer": "rev@acme.test"}

        wrong_body = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": False, "comments": "not an approval"},
            headers=review_headers,
        )
        assert wrong_body.status_code == 400

        approved = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "ship it"},
            headers=review_headers,
        )
        assert approved.status_code == 200, approved.text

        # A second approval must not resume a completed graph or mint a token.
        repeated = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "again"},
            headers=review_headers,
        )
        assert repeated.status_code == 409
        assert "must be 'reviewing'" in repeated.json()["detail"]

        # The reject endpoint is only valid while the campaign is reviewing.
        rejected = await api_client.post(
            f"/reviews/{campaign['id']}/reject",
            json={"approved": False, "comments": "too late"},
            headers=review_headers,
        )
        assert rejected.status_code == 409

    async def test_approval_survives_post_approval_analytics_failure(
        self, api_client, tenant_headers, graph_llm, monkeypatch
    ):
        import adforge.agents.kpi_analytics.agent as kpi_agent_module

        async def failing_fetch(*args, **kwargs):
            raise RuntimeError("no ad account credentials configured")

        monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", failing_fetch)

        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])

        approved = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "approved"},
            headers={**headers, "X-AdForge-Reviewer": "rev@acme.test"},
        )

        assert approved.status_code == 200, approved.text
        body = approved.json()
        # Analytics can only run after a real campaign is live. A missing ad
        # account must not revoke the human approval or block publishing.
        assert body["status"] == "approved", body
        assert body["approval_token_issued"] is True

        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["status"] == "approved"

    @respx.mock
    async def test_approve_then_publish_spends_once(
        self, api_client, tenant_headers, graph_llm, monkeypatch
    ):
        from adforge.config import get_settings

        monkeypatch.setenv("ADFORGE_ENABLE_LIVE_WRITES", "true")
        monkeypatch.setenv("ADFORGE_PUBLISH_WEBHOOK_URL", "https://ads.example.test/publish")
        get_settings.cache_clear()

        headers = tenant_headers("acme")
        review_headers = {**headers, "X-AdForge-Reviewer": "rev@acme.test"}
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])

        approved = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "approved"},
            headers=review_headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert "approval_token" not in approved.json()

        webhook = respx.post("https://ads.example.test/publish").mock(
            return_value=httpx.Response(200, json={"external_campaign_id": "ext-1"})
        )

        published = await api_client.post(
            f"/reviews/{campaign['id']}/publish", headers=review_headers
        )

        assert published.status_code == 200, published.text
        body = published.json()
        assert body["status"] == "live"
        assert len(body["published"]) == 2
        # One adapter call per channel allocation.
        assert webhook.call_count == 2
        sent_channels = {json.loads(call.request.content)["channel"] for call in webhook.calls}
        assert sent_channels == {"google", "linkedin"}

        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["status"] == "live"

        # The single-use token was consumed: a second publish must not spend.
        again = await api_client.post(f"/reviews/{campaign['id']}/publish", headers=review_headers)
        assert again.status_code == 409
        assert webhook.call_count == 2

    async def test_reject_routes_back_and_re_reviews(self, api_client, tenant_headers, graph_llm):
        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])

        reject = await api_client.post(
            f"/reviews/{campaign['id']}/reject",
            json={"approved": False, "comments": "Make it less generic"},
            headers={**headers, "X-AdForge-Reviewer": "rev@acme.test"},
        )
        assert reject.status_code == 200, reject.text
        assert reject.json()["decision"] == "rejected"
        # The revised creative is paused for another review round.
        assert reject.json()["status"] == "reviewing"

        pending = (await api_client.get("/reviews/pending", headers=headers)).json()
        assert pending["count"] == 1

        # Reviewer comments reached the creative prompt.
        assert any(
            "Make it less generic" in str(m.get("content", ""))
            for call in graph_llm.calls
            for m in call
        )

    async def test_three_rejections_block_the_campaign(self, api_client, tenant_headers, graph_llm):
        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])

        for _ in range(3):
            response = await api_client.post(
                f"/reviews/{campaign['id']}/reject",
                json={"approved": False, "comments": "Still not right"},
                headers={**headers, "X-AdForge-Reviewer": "rev@acme.test"},
            )
            assert response.status_code == 200, response.text

        assert response.json()["status"] == "blocked"
        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["status"] == "blocked"

    async def test_reviewer_allowlist_enforced(
        self, api_client, tenant_headers, graph_llm, monkeypatch
    ):
        from adforge.config import get_settings

        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])

        monkeypatch.setenv("APP_REVIEWER_ALLOWLIST", "boss@acme.test, lead@acme.test")
        get_settings.cache_clear()

        denied = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": ""},
            headers={**headers, "X-AdForge-Reviewer": "intern@acme.test"},
        )
        assert denied.status_code == 403

        no_reviewer = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": ""},
            headers=headers,
        )
        assert no_reviewer.status_code == 403

        allowed = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": ""},
            headers={**headers, "X-AdForge-Reviewer": "BOSS@acme.test"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "approved"

    async def test_other_tenant_cannot_approve_or_inspect(
        self, api_client, tenant_headers, graph_llm
    ):
        owner = tenant_headers("acme")
        intruder = tenant_headers("intruder")
        campaign = await _create_campaign(api_client, owner)
        await _run_campaign(api_client, owner, campaign["id"])

        packet = await api_client.get(f"/reviews/{campaign['id']}/packet", headers=intruder)
        assert packet.status_code == 404

        approve = await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": ""},
            headers={**intruder, "X-AdForge-Reviewer": "x@evil.test"},
        )
        assert approve.status_code == 404

    async def test_critical_compliance_auto_blocks(self, api_client, tenant_headers, monkeypatch):
        from support import ScriptedLLM, default_agent_responses

        responses = default_agent_responses()
        responses["Produce a CreativeConcept JSON object"] = {
            **responses["Produce a CreativeConcept JSON object"],
            "headline": "Cheap cigarettes delivered fast",
            "primary_text": "Buy cigarettes and vape products online today.",
        }
        responses["Produce a ComplianceVerdict JSON object"] = {
            "approved": False,
            "risk_level": "critical",
            "flagged_issues": ["Tobacco advertising is prohibited"],
            "required_disclosures": [],
            "reviewer_notes": "Hard block: tobacco.",
            "blocked_terms": ["BLOCK_TOBACCO"],
        }
        llm = ScriptedLLM(responses=responses)
        import adforge.graph.state as graph_state

        for agent in (
            graph_state._research,
            graph_state._creative,
            graph_state._media_planner,
            graph_state._compliance,
        ):
            monkeypatch.setattr(agent, "_llm", llm, raising=False)

        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        run = await _run_campaign(api_client, headers, campaign["id"])
        assert run["status"] == "blocked"
        assert run["paused_for_review"] is False
        assert run["final_state"]["review_status"] == "auto_blocked"

        fetched = (await api_client.get(f"/campaigns/{campaign['id']}", headers=headers)).json()
        assert fetched["status"] == "blocked"

        pending = (await api_client.get("/reviews/pending", headers=headers)).json()
        assert pending["count"] == 0


class TestTraces:
    async def test_agent_runs_and_audit_events_are_recorded(
        self, api_client, tenant_headers, graph_llm
    ):
        headers = tenant_headers("acme")
        campaign = await _create_campaign(api_client, headers)
        await _run_campaign(api_client, headers, campaign["id"])
        await api_client.post(
            f"/reviews/{campaign['id']}/approve",
            json={"approved": True, "comments": "ok"},
            headers={**headers, "X-AdForge-Reviewer": "rev@acme.test"},
        )

        runs_response = await api_client.get(f"/traces/{campaign['id']}/runs", headers=headers)
        assert runs_response.status_code == 200
        runs = runs_response.json()
        assert runs["count"] >= 7
        names = {run["agent_name"] for run in runs["runs"]}
        assert {
            "research",
            "creative_strategist",
            "media_planner",
            "compliance",
            "kpi_analytics",
            "roi_calculator",
            "optimization",
        }.issubset(names)

        filtered = await api_client.get(
            f"/traces/{campaign['id']}/runs?agent_name=compliance", headers=headers
        )
        assert filtered.json()["count"] == 1

        audit_response = await api_client.get(f"/traces/{campaign['id']}/audit", headers=headers)
        assert audit_response.status_code == 200
        events = audit_response.json()["events"]
        event_types = {event["event_type"] for event in events}
        assert "agent.start" in event_types
        assert "llm.call" in event_types
        assert "review.decision" in event_types
        assert "kpi.fetch" in event_types

        decisions = await api_client.get(
            f"/traces/{campaign['id']}/audit?event_type=review.decision",
            headers=headers,
        )
        assert decisions.json()["count"] == 1

        # Traces are tenant-scoped.
        other = await api_client.get(
            f"/traces/{campaign['id']}/runs", headers=tenant_headers("other")
        )
        assert other.json()["count"] == 0
