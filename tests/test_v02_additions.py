"""Tests for v0.2 additions: HITL review, read-only enforcement, public-data MCP."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from adforge.mcp_servers import permissions, public_data, ads


class TestPermissions:
    def test_readonly_decorator_marks_function(self):
        @permissions.readonly
        async def my_read_tool():
            return "ok"
        assert my_read_tool.__adforge_permission__ == "readonly"

    def test_write_operation_decorator_marks_function(self):
        @permissions.write_operation(requires_approval=True)
        async def my_write_tool(headers=None):
            return json.dumps({"ok": True})
        assert my_write_tool.__adforge_permission__ == "write"
        assert my_write_tool.__adforge_requires_approval__ is True

    @pytest.mark.asyncio
    async def test_write_operation_blocks_without_token(self, monkeypatch):
        # Ensure no APP_SECRET is set — no approval can be valid
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("APP_SECRET", "test-secret-12345")

        @permissions.write_operation(requires_approval=True)
        async def publish(channel, headers=None):
            return json.dumps({"published": True})

        # No approval token in headers → must be blocked
        result = await publish("google", headers={"X-AdForge-Tenant": "acme"})
        data = json.loads(result)
        assert data.get("error") == "approval_required"
        assert data.get("operation") == "publish"

    @pytest.mark.asyncio
    async def test_issue_approval_token_round_trip(self, monkeypatch):
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("APP_SECRET", "test-secret-12345")
        campaign_id = str(uuid4())

        token = permissions.issue_approval_token(
            campaign_id=campaign_id,
            operation="publish",
            reviewer_email="reviewer@example.com",
            ttl_seconds=60,
        )
        assert campaign_id in token
        assert "reviewer@example.com" in token

        # Verify the token
        assert permissions._verify_approval_token(token, campaign_id, "publish") is True
        # Wrong campaign must fail
        assert permissions._verify_approval_token(token, str(uuid4()), "publish") is False
        # Wrong operation must fail
        assert permissions._verify_approval_token(token, campaign_id, "pause") is False

    @pytest.mark.asyncio
    async def test_write_operation_succeeds_with_valid_token(self, monkeypatch):
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("APP_SECRET", "test-secret-12345")
        campaign_id = str(uuid4())

        token = permissions.issue_approval_token(
            campaign_id=campaign_id,
            operation="publish_campaign",
            reviewer_email="reviewer@example.com",
            ttl_seconds=60,
        )

        # The ads MCP's publish_campaign uses @write_operation
        result = await ads.publish_campaign(
            channel="google",
            campaign_name="test",
            budget_usd=1000.0,
            creative={"headline": "Test"},
            audience={"age": "25-44"},
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Campaign": campaign_id,
                "X-AdForge-Approval-Token": token,
            },
        )
        data = json.loads(result)
        assert "external_campaign_id" in data
        assert data["published_with_approval"] is True


class TestAdsMcpPermissions:
    """Every write tool on the ads MCP must be marked @write_operation."""

    def test_publish_campaign_is_write_gated(self):
        assert ads.publish_campaign.__adforge_permission__ == "write"

    def test_pause_campaign_is_write_gated(self):
        assert ads.pause_campaign.__adforge_permission__ == "write"

    def test_update_budget_is_write_gated(self):
        assert ads.update_budget.__adforge_permission__ == "write"

    def test_list_channels_is_readonly(self):
        assert ads.list_channels.__adforge_permission__ == "readonly"


class TestPublicDataMcp:
    @pytest.mark.asyncio
    async def test_appstore_search_returns_real_data(self):
        """iTunes Search API is public + requires no auth — should return real apps."""
        result = await public_data.appstore_search(
            "slack", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        # We can't assert specific app names (results vary), but the API should succeed
        assert "apps" in data
        assert data.get("country") == "us"

    @pytest.mark.asyncio
    async def test_ipinfo_lookup_works_without_token(self):
        """IPInfo returns limited data without a token but doesn't error."""
        # 8.8.8.8 is Google DNS — definitely geolocatable
        result = await public_data.ipinfo_lookup(
            "8.8.8.8", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data.get("ip") == "8.8.8.8"
        # Without token, ipinfo may return city/country — depends on their rate limit
        assert "country" in data or "error" in data

    @pytest.mark.asyncio
    async def test_github_repo_stats_returns_real_data(self):
        """GitHub public API requires no auth (60/hr/IP limit)."""
        result = await public_data.github_repo_stats(
            "langchain-ai/langgraph", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert "repo" in data
        assert data["repo"] == "langchain-ai/langgraph"
        # stars should be a positive integer
        assert isinstance(data.get("stars"), int)
        assert data["stars"] > 1000  # langgraph definitely has >1k stars

    @pytest.mark.asyncio
    async def test_abuseipdb_returns_mock_without_key(self, monkeypatch):
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.delenv("ABUSEIPDB_API_KEY", raising=False)
        result = await public_data.abuseipdb_check(
            "1.2.3.4", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data.get("mock") is True

    @pytest.mark.asyncio
    async def test_similarweb_returns_mock_without_key(self, monkeypatch):
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.delenv("SIMILARWEB_API_KEY", raising=False)
        result = await public_data.similarweb_traffic(
            "example.com", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data.get("mock") is True
        assert "visits_30d" in data

    def test_all_public_data_tools_are_readonly(self):
        """Verify no public-data tool can mutate external state."""
        for tool_name in ["similarweb_traffic", "appstore_search", "appstore_reviews",
                          "ipinfo_lookup", "abuseipdb_check", "github_repo_stats"]:
            tool = getattr(public_data, tool_name)
            assert tool.__adforge_permission__ == "readonly", \
                f"{tool_name} must be @readonly"


class TestGraphWithHitl:
    def test_graph_includes_human_review_node(self):
        from adforge.graph.state import build_graph
        builder = build_graph()
        assert "human_review" in builder.nodes

    def test_route_after_compliance_goes_to_human_review(self):
        from adforge.graph.state import route_after_compliance
        # Regardless of compliance verdict, we route through HITL (unless auto_blocked)
        state = {"compliance_verdict": {"approved": True, "risk_level": "low"}}
        assert route_after_compliance(state) == "human_review"

    def test_route_after_review_approved(self):
        from adforge.graph.state import route_after_review
        state = {"review_status": "approved"}
        assert route_after_review(state) == "kpi_analytics"

    def test_route_after_review_rejected_routes_to_creative(self):
        from adforge.graph.state import route_after_review
        state = {"review_status": "rejected", "revision_count": 1}
        assert route_after_review(state) == "creative_strategist"

    def test_route_after_review_rejected_max_revisions_ends(self):
        from adforge.graph.state import route_after_review
        state = {"review_status": "rejected", "revision_count": 3}
        assert route_after_review(state) == "__end__"

    def test_route_after_review_auto_blocked_ends(self):
        from adforge.graph.state import route_after_review, END
        state = {"review_status": "auto_blocked"}
        assert route_after_review(state) == END

    def test_route_after_review_skipped_in_dev(self):
        from adforge.graph.state import route_after_review
        state = {"review_status": "skipped"}
        assert route_after_review(state) == "kpi_analytics"
