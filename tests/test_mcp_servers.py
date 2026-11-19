"""Tests for the MCP server tools — exercise the FastMCP tool functions directly."""

from __future__ import annotations

import json

import pytest

from adforge.mcp_servers import compliance, research, roi


class TestResearchMcp:
    @pytest.mark.asyncio
    async def test_web_search_requires_tenant_header(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await research.web_search("test query", headers=None)

    @pytest.mark.asyncio
    async def test_web_search_uses_duckduckgo_first(self, monkeypatch):
        async def fake_ddg(query, max_results):
            return [{"title": "CRM guide", "url": "https://example.com/crm", "snippet": "Best CRM"}]

        async def fail_wiki(query, n):
            raise AssertionError("wikipedia must not be reached")

        monkeypatch.setattr(research, "_duckduckgo_search", fake_ddg)
        monkeypatch.setattr(research, "_wikipedia_search", fail_wiki)

        result = await research.web_search(
            "best CRM for startups", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data["provider"] == "duckduckgo"
        assert data["results"][0]["title"] == "CRM guide"

    @pytest.mark.asyncio
    async def test_web_search_falls_back_to_wikipedia(self, monkeypatch):
        async def empty_ddg(query, max_results):
            return []

        async def empty_ddg_lite(query, max_results):
            return []

        async def empty_bing_rss(query, max_results):
            return []

        async def fake_wiki(query, n):
            return [{"title": "CRM", "url": "https://en.wikipedia.org/wiki/CRM", "snippet": ""}]

        monkeypatch.setattr(research, "_duckduckgo_search", empty_ddg)
        monkeypatch.setattr(research, "_ddg_lite_search", empty_ddg_lite)
        monkeypatch.setattr(research, "_bing_rss_search", empty_bing_rss)
        monkeypatch.setattr(research, "_wikipedia_search", fake_wiki)
        result = await research.web_search("CRM", headers={"X-AdForge-Tenant": "acme"})
        assert json.loads(result)["provider"] == "wikipedia"

    @pytest.mark.asyncio
    async def test_web_search_reports_no_provider_when_all_fail(self, monkeypatch):
        async def empty(query, n):
            return []

        async def no_tavily(query, n):
            return {"error": "no key"}

        monkeypatch.setattr(research, "_duckduckgo_search", empty)
        monkeypatch.setattr(research, "_ddg_lite_search", empty)
        monkeypatch.setattr(research, "_bing_rss_search", empty)
        monkeypatch.setattr(research, "_wikipedia_search", empty)
        monkeypatch.setattr(research, "_tavily_search", no_tavily)
        result = await research.web_search(
            "obscure query", max_results=3, headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data["provider"] == "none"
        assert data["results"] == []
        assert "no simulated results" in data["error"]

    @pytest.mark.asyncio
    async def test_keyword_clusters_returns_n_keywords(self, monkeypatch):
        # Google Suggest is a live endpoint; mock the transport so the unit
        # tier stays hermetic (a CI runner with no egress must still pass).
        async def fake_suggest(query: str, n: int) -> list[str]:
            assert query == "project management"
            return ["project management software", "project management tools"][:n]

        monkeypatch.setattr(research, "_google_suggest", fake_suggest)
        result = await research.keyword_clusters(
            "project management", n=10, headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data["clusters"] == [
            "project management software",
            "project management tools",
        ]
        assert data["seed"] == "project management"
        assert data["source"] == "google_suggest"


class TestRoiMcp:
    @pytest.mark.asyncio
    async def test_compute_roas(self):
        result = await roi.compute_roas(revenue_usd=1000, spend_usd=250)
        assert json.loads(result)["roas"] == 4.0

    @pytest.mark.asyncio
    async def test_compute_roas_rejects_zero_spend(self):
        result = await roi.compute_roas(revenue_usd=1000, spend_usd=0)
        assert "error" in json.loads(result)

    @pytest.mark.asyncio
    async def test_classify_recommendation_scale(self):
        result = await roi.classify_recommendation(roas=5.0, payback_days=20)
        assert json.loads(result)["recommendation"] == "scale"

    @pytest.mark.asyncio
    async def test_classify_recommendation_kill(self):
        result = await roi.classify_recommendation(roas=0.5, payback_days=None)
        assert json.loads(result)["recommendation"] == "kill"


class TestComplianceMcp:
    @pytest.mark.asyncio
    async def test_evaluate_creative_requires_tenant(self):
        result = await compliance.evaluate_creative("Great product, buy now!", headers=None)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_evaluate_creative_blocks_tobacco(self):
        result = await compliance.evaluate_creative(
            "Buy cigarettes online!", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert data["approved"] is False
        assert data["risk_level"] == "critical"

    @pytest.mark.asyncio
    async def test_list_blocked_terms(self):
        result = await compliance.list_blocked_terms(headers={"X-AdForge-Tenant": "acme"})
        data = json.loads(result)
        assert len(data["blocked"]) > 0
        assert any(r["rule_id"] == "BLOCK_TOBACCO" for r in data["blocked"])

    @pytest.mark.asyncio
    async def test_list_required_disclosures(self):
        result = await compliance.list_required_disclosures(headers={"X-AdForge-Tenant": "acme"})
        data = json.loads(result)
        assert data["disclosures"]
        assert any(row["required_disclosure"] for row in data["disclosures"])
        assert all(row["rule_id"] for row in data["disclosures"])
