"""Offline contract tests for the real ad-platform reporting clients.

Every payload is a hand-vendored JSON fixture under ``tests/fixtures``. The
base URLs are pointed at documentation-only hostnames so the tests also prove
that no platform endpoint is hard-coded in the fetcher.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

import adforge.agents.kpi_analytics.fetcher as fetcher
from adforge.agents.kpi_analytics.fetcher import (
    fetch_google,
    fetch_linkedin,
    fetch_meta,
    fetch_tiktok,
)
from adforge.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures" / "ad_platforms"
START = date(2026, 1, 2)
END = date(2026, 1, 3)
CAMPAIGN_ID = uuid4()


def _fixture(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _creds(monkeypatch) -> None:
    values = {
        "APP_SECRET": "test-secret-not-for-production",
        "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token",
        "GOOGLE_ADS_CLIENT_ID": "client-id",
        "GOOGLE_ADS_CLIENT_SECRET": "client-secret",
        "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token",
        "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        "GOOGLE_ADS_API_BASE": "https://google.example.test",
        "GOOGLE_ADS_API_VERSION": "v99",
        "GOOGLE_OAUTH_TOKEN_URL": "https://oauth.example.test/token",
        "META_ACCESS_TOKEN": "meta-token",
        "META_AD_ACCOUNT_ID": "act_456",
        "META_GRAPH_BASE": "https://meta.example.test",
        "META_GRAPH_API_VERSION": "v99.0",
        "LINKEDIN_ACCESS_TOKEN": "li-token",
        "LINKEDIN_AD_ACCOUNT_ID": "li-account",
        "LINKEDIN_API_BASE": "https://linkedin.example.test",
        "LINKEDIN_API_VERSION": "209901",
        "TIKTOK_ACCESS_TOKEN": "tt-token",
        "TIKTOK_ADVERTISER_ID": "tt-advertiser",
        "TIKTOK_API_BASE": "https://tiktok.example.test",
        "TIKTOK_API_VERSION": "v9.9",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    fetcher._google_token_cache.update(access_token=None, expires_at=0.0)


@respx.mock
async def test_google_search_stream_contract(monkeypatch):
    _creds(monkeypatch)
    respx.post("https://oauth.example.test/token").mock(
        return_value=httpx.Response(200, json={"access_token": "google-access", "expires_in": 3600})
    )
    search = respx.post(
        "https://google.example.test/v99/customers/1234567890/googleAds:searchStream"
    ).mock(return_value=httpx.Response(200, json=_fixture("google_search_stream.json")))

    async with httpx.AsyncClient() as client:
        rows = await fetch_google(
            client,
            campaign_id=CAMPAIGN_ID,
            external_id="123",
            start=START,
            end=END,
        )

    assert len(rows) == 2
    assert [row.platform_date for row in rows] == [START, END]
    assert rows[0].impressions == 1000
    assert rows[0].spend_usd == 5.0
    assert rows[0].revenue_usd == 300.0
    assert rows[0].source == "google_ads"
    assert rows[0].is_mock is False

    request = search.calls[0].request
    assert request.headers["developer-token"] == "dev-token"
    assert request.headers["authorization"] == "Bearer google-access"
    query = json.loads(request.content)["query"]
    assert "campaign.id = 123" in query
    assert "BETWEEN '2026-01-02' AND '2026-01-03'" in query


@respx.mock
async def test_meta_insights_contract(monkeypatch):
    _creds(monkeypatch)
    route = respx.get("https://meta.example.test/v99.0/act_456/insights").mock(
        return_value=httpx.Response(200, json=_fixture("meta_insights.json"))
    )

    async with httpx.AsyncClient() as client:
        rows = await fetch_meta(
            client,
            campaign_id=CAMPAIGN_ID,
            external_id="456",
            start=START,
            end=END,
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "meta"
    assert row.impressions == 2000
    assert row.clicks == 80
    assert row.spend_usd == 12.5
    # Only conversion action types count; the page-view action is ignored.
    assert row.conversions == 8
    assert row.revenue_usd == 400.0
    assert row.source == "meta_ads"

    params = route.calls[0].request.url.params
    assert params["level"] == "campaign"
    assert params["time_increment"] == "1"
    assert json.loads(params["time_range"]) == {
        "since": "2026-01-02",
        "until": "2026-01-03",
    }


@respx.mock
async def test_linkedin_ad_analytics_contract(monkeypatch):
    _creds(monkeypatch)
    route = respx.get("https://linkedin.example.test/rest/adAnalytics").mock(
        return_value=httpx.Response(200, json=_fixture("linkedin_ad_analytics.json"))
    )

    async with httpx.AsyncClient() as client:
        rows = await fetch_linkedin(
            client,
            campaign_id=CAMPAIGN_ID,
            external_id="777",
            start=START,
            end=END,
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "linkedin"
    assert row.platform_date == START
    assert row.impressions == 1500
    assert row.spend_usd == 9.5
    assert row.revenue_usd == 240.0
    assert row.source == "linkedin_ads"

    request = route.calls[0].request
    assert request.headers["linkedin-version"] == "209901"
    assert request.headers["x-restli-protocol-version"] == "2.0.0"
    params = request.url.params
    assert params["q"] == "analytics"
    assert params["timeGranularity"] == "DAILY"
    assert "urn:li:sponsoredCampaign:777" in params["campaigns"]


@respx.mock
async def test_tiktok_report_contract(monkeypatch):
    _creds(monkeypatch)
    route = respx.get("https://tiktok.example.test/open_api/v9.9/report/integrated/get/").mock(
        return_value=httpx.Response(200, json=_fixture("tiktok_report.json"))
    )

    async with httpx.AsyncClient() as client:
        rows = await fetch_tiktok(
            client,
            campaign_id=CAMPAIGN_ID,
            external_id="789",
            start=START,
            end=END,
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "tiktok"
    assert row.campaign_external_id == "789"
    assert row.impressions == 2500
    assert row.spend_usd == 20.0
    assert row.revenue_usd == 500.0
    assert row.downloads == 7
    assert row.source == "tiktok_ads"

    request = route.calls[0].request
    assert request.headers["access-token"] == "tt-token"
    params = request.url.params
    assert params["data_level"] == "AUCTION_CAMPAIGN"
    assert json.loads(params["dimensions"]) == ["campaign_id", "stat_time_day"]


@respx.mock
async def test_platform_errors_are_explicit(monkeypatch):
    _creds(monkeypatch)
    respx.get("https://meta.example.test/v99.0/act_456/insights").mock(
        return_value=httpx.Response(503, text="upstream down")
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(fetcher.AdPlatformError, match="Meta insights failed"):
            await fetch_meta(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="456",
                start=START,
                end=END,
            )


@respx.mock
async def test_webhook_publish_fixture_drives_write_gateway(monkeypatch):
    from adforge.mcp_servers.ads import writes

    monkeypatch.setenv("ADFORGE_ENABLE_LIVE_WRITES", "true")
    monkeypatch.setenv("ADFORGE_PUBLISH_WEBHOOK_URL", "https://adapter.example.test/publish")
    get_settings.cache_clear()
    route = respx.post("https://adapter.example.test/publish").mock(
        return_value=httpx.Response(200, json=_fixture("webhook_publish.json"))
    )

    result = await writes.publish_campaign(
        channel="google",
        campaign_name="Acme",
        budget_usd=500.0,
        creative={"headline": "Test"},
        audience={"age": "28-45"},
    )

    assert result["external_campaign_id"] == "ext-123"
    assert result["status"] == "paused"
    body = json.loads(route.calls[0].request.content)
    assert body["action"] == "publish_campaign"
    assert body["channel"] == "google"
    assert body["budget_usd"] == 500.0
