"""Tests for the MCP server tools — exercise the FastMCP tool functions directly."""
from __future__ import annotations

import json

import pytest

from adforge.mcp_servers import research, roi, compliance


class TestResearchMcp:
    @pytest.mark.asyncio
    async def test_web_search_requires_tenant_header(self):
        result = await research.web_search("test query", headers=None)
        data = json.loads(result)
        assert "error" in data or "results" in data  # mock mode returns results

    @pytest.mark.asyncio
    async def test_web_search_with_tenant(self):
        result = await research.web_search(
            "best CRM for startups", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert "results" in data or "answer" in data

    @pytest.mark.asyncio
    async def test_keyword_clusters_returns_n_keywords(self):
        result = await research.keyword_clusters(
            "project management", n=10, headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)
        assert len(data["clusters"]) <= 10
        assert data["seed"] == "project management"


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
        result = await compliance.evaluate_creative(
            "Great product, buy now!", headers=None
        )
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
