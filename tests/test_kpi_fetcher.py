"""Unit tests for the real ad-platform KPI fetchers (no live credentials).

Every provider is exercised against recorded HTTP shapes via ``respx`` so the
normalisation + error paths are deterministic. Persistence is covered by the
embedded-Postgres integration suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest
import respx

import adforge.agents.kpi_analytics.fetcher as fetcher
from adforge.agents.kpi_analytics.fetcher import (
    AdPlatformError,
    _as_date,
    _as_float,
    _as_int,
    _ratio,
    build_snapshot,
    fetch_all_channels,
    fetch_channel_snapshots,
    fetch_google,
    fetch_linkedin,
    fetch_meta,
    fetch_tiktok,
    persist_snapshots,
)
from adforge.guardrails.data_isolation import TenantScope

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
LINKEDIN_ANALYTICS_URL = "https://api.linkedin.com/rest/adAnalytics"

START = date(2026, 1, 1)
END = date(2026, 1, 3)
CAMPAIGN_ID = uuid4()


def _scope() -> TenantScope:
    return TenantScope(tenant_id="acme", campaign_id=CAMPAIGN_ID)


def _creds(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    from adforge.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_google_token():
    fetcher._google_token_cache["access_token"] = None
    fetcher._google_token_cache["expires_at"] = 0.0
    yield
    fetcher._google_token_cache["access_token"] = None
    fetcher._google_token_cache["expires_at"] = 0.0


class TestNormalisationHelpers:
    def test_as_float(self):
        assert _as_float("1.5") == 1.5
        assert _as_float(None, 2.0) == 2.0
        assert _as_float("nope", 3.0) == 3.0

    def test_as_int_rounds_and_defaults(self):
        assert _as_int("2.6") == 3
        assert _as_int(None, 7) == 7
        assert _as_int("boo", 8) == 8

    @pytest.mark.parametrize(
        "value,expected",
        [
            (date(2026, 1, 2), date(2026, 1, 2)),
            (datetime(2026, 1, 3, 5, tzinfo=UTC), date(2026, 1, 3)),
            ("2026-01-04", date(2026, 1, 4)),
            ("2026-01-05 12:00:00", date(2026, 1, 5)),
            ("20260106", date(2026, 1, 6)),
            ("2026-01-07T08:09:10Z", date(2026, 1, 7)),
        ],
    )
    def test_as_date_formats(self, value, expected):
        assert _as_date(value) == expected

    def test_as_date_invalid(self):
        assert _as_date("not a date") is None
        assert _as_date("") is None
        assert _as_date(None) is None

    def test_ratio_is_bounded_and_safe(self):
        assert _ratio(1, 4) == 0.25
        assert _ratio(5, 4) == 1.0
        assert _ratio(1, 0) == 0.0


class TestBuildSnapshot:
    def test_derived_metrics(self):
        snapshot = build_snapshot(
            channel="google",
            campaign_id=CAMPAIGN_ID,
            campaign_external_id="ext-1",
            platform_date=START,
            impressions=1000,
            clicks=100,
            spend_usd=50.0,
            conversions=10,
            revenue_usd=200.0,
            source="google_ads",
        )
        assert snapshot.ctr == 0.1
        assert snapshot.cvr == 0.1
        assert snapshot.cpc == 0.5
        assert snapshot.cpa == 5.0
        assert snapshot.cpm == 50.0
        assert snapshot.roas == 4.0
        assert snapshot.captured_at.date() == START

    def test_negative_values_are_clamped(self):
        snapshot = build_snapshot(
            channel="meta",
            campaign_id=CAMPAIGN_ID,
            campaign_external_id=None,
            platform_date=START,
            impressions=-10,
            clicks=-5,
            spend_usd=-1.0,
            conversions=-2,
            revenue_usd=-3.0,
            downloads=-4,
            source="meta_ads",
        )
        assert snapshot.impressions == 0
        assert snapshot.clicks == 0
        assert snapshot.spend_usd == 0
        assert snapshot.conversions == 0
        assert snapshot.revenue_usd == 0
        assert snapshot.downloads == 0
        assert snapshot.roas == 0.0


class TestDispatchGuards:
    async def test_missing_external_id_raises(self, monkeypatch):
        # Disable the dev/test mock path so the missing-external-id guard fires.
        _creds(
            monkeypatch,
            GOOGLE_ADS_USE_MOCK=False,
            META_USE_MOCK=False,
            LINKEDIN_USE_MOCK=False,
            TIKTOK_USE_MOCK=False,
        )
        for channel in ("google", "meta", "linkedin", "tiktok"):
            with pytest.raises(AdPlatformError, match="campaign_external_id"):
                await fetch_channel_snapshots(_scope(), channel, CAMPAIGN_ID, start=START, end=END)

    async def test_unsupported_channel_raises(self):
        with pytest.raises(AdPlatformError, match="Unsupported"):
            await fetch_channel_snapshots(_scope(), "pinterest", CAMPAIGN_ID, start=START, end=END)

    async def test_end_before_start_raises(self):
        with pytest.raises(AdPlatformError, match="end date"):
            await fetch_channel_snapshots(
                _scope(),
                "google",
                CAMPAIGN_ID,
                external_id="1",
                start=END,
                end=START,
            )

    async def test_days_back_defaults_to_seven(self, monkeypatch):
        # Keep the patched fetch_google reachable by disabling the mock path.
        _creds(monkeypatch, GOOGLE_ADS_USE_MOCK=False)
        captured = {}

        async def fake_fetch(client, *, campaign_id, external_id, start, end):
            captured.update(start=start, end=end)
            return []

        monkeypatch.setattr(fetcher, "fetch_google", fake_fetch)
        await fetch_channel_snapshots(_scope(), "google", CAMPAIGN_ID, external_id="1")
        assert (captured["end"] - captured["start"]).days == 6

    async def test_fetch_all_channels_returns_latest(self, monkeypatch):
        # Without this, the mock branch would return its own snapshots and the
        # patched fetch_google below would never run.
        _creds(monkeypatch, GOOGLE_ADS_USE_MOCK=False)

        async def fake_fetch(client, *, campaign_id, external_id, start, end):
            return [
                build_snapshot(
                    channel="google",
                    campaign_id=campaign_id,
                    campaign_external_id=external_id,
                    platform_date=day,
                    impressions=1,
                    clicks=1,
                    spend_usd=1.0,
                    conversions=0,
                    revenue_usd=0.0,
                    source="google_ads",
                )
                for day in (start, end)
            ]

        monkeypatch.setattr(fetcher, "fetch_google", fake_fetch)
        snapshot = await fetch_all_channels(
            _scope(), "google", CAMPAIGN_ID, external_id="1", start=START, end=END
        )
        assert snapshot is not None
        assert snapshot.platform_date == END


class TestGoogle:
    async def _creds(self, monkeypatch, **overrides):
        values = {
            "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token",
            "GOOGLE_ADS_CLIENT_ID": "client-id",
            "GOOGLE_ADS_CLIENT_SECRET": "secret",
            "GOOGLE_ADS_REFRESH_TOKEN": "refresh",
            "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        }
        values.update(overrides)
        _creds(monkeypatch, **values)

    @respx.mock
    async def test_success(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        route = respx.post(
            "https://googleads.googleapis.com/v21/customers/1234567890/googleAds:searchStream"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "results": [
                            {
                                "segments": {"date": "2026-01-01"},
                                "metrics": {
                                    "impressions": 1000,
                                    "clicks": 100,
                                    "costMicros": 5_000_000,
                                    "conversions": 10,
                                    "conversionsValue": 250.0,
                                },
                            }
                        ]
                    }
                ],
            )
        )
        async with httpx.AsyncClient() as client:
            snapshots = await fetch_google(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="987",
                start=START,
                end=END,
            )
        assert len(snapshots) == 1
        assert snapshots[0].spend_usd == 5.0
        assert snapshots[0].revenue_usd == 250.0
        assert snapshots[0].source == "google_ads"
        request = route.calls[0].request
        assert request.headers["developer-token"] == "dev-token"
        assert request.headers["Authorization"] == "Bearer tok"

    @respx.mock
    async def test_token_is_cached(self, monkeypatch):
        await self._creds(monkeypatch)
        token_route = respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok"})
        )
        respx.post(url__regex=r"https://googleads\.googleapis\.com/.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with httpx.AsyncClient() as client:
            await fetch_google(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="1",
                start=START,
                end=END,
            )
            await fetch_google(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="1",
                start=START,
                end=END,
            )
        assert token_route.call_count == 1

    async def test_missing_credentials_raises(self, monkeypatch):
        await self._creds(monkeypatch, GOOGLE_ADS_DEVELOPER_TOKEN="")
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="GOOGLE_ADS_DEVELOPER_TOKEN"):
                await fetch_google(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_token_failure(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(400, text="bad refresh token")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="OAuth token refresh failed"):
                await fetch_google(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_token_response_without_access_token(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="no access_token"):
                await fetch_google(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_search_failure(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok"})
        )
        respx.post(url__regex=r"https://googleads\.googleapis\.com/.*").mock(
            return_value=httpx.Response(403, text="permission denied")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="searchStream failed"):
                await fetch_google(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_skips_rows_without_date(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok"})
        )
        respx.post(url__regex=r"https://googleads\.googleapis\.com/.*").mock(
            return_value=httpx.Response(200, json={"results": [{"segments": {}, "metrics": {}}]})
        )
        async with httpx.AsyncClient() as client:
            snapshots = await fetch_google(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="1",
                start=START,
                end=END,
            )
        assert snapshots == []


class TestMeta:
    async def _creds(self, monkeypatch, **overrides):
        values = {
            "META_ACCESS_TOKEN": "meta-token",
            "META_AD_ACCOUNT_ID": "act_123",
        }
        values.update(overrides)
        _creds(monkeypatch, **values)

    @respx.mock
    async def test_success_filters_to_campaign(self, monkeypatch):
        await self._creds(monkeypatch)
        route = respx.get(url__regex=r"https://graph\.facebook\.com/.*/insights").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "campaign_id": "987",
                            "date_start": "2026-01-01",
                            "impressions": "1000",
                            "clicks": "50",
                            "spend": "12.5",
                            "actions": [
                                {"action_type": "purchase", "value": "4"},
                                {"action_type": "like", "value": "9"},
                            ],
                            "action_values": [{"action_type": "purchase", "value": "80.0"}],
                        },
                        {
                            "campaign_id": "other",
                            "date_start": "2026-01-01",
                        },
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as client:
            snapshots = await fetch_meta(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="987",
                start=START,
                end=END,
            )
        assert len(snapshots) == 1
        assert snapshots[0].conversions == 4
        assert snapshots[0].revenue_usd == 80.0
        assert snapshots[0].channel == "meta"
        assert route.calls[0].request.url.params["access_token"] == "meta-token"

    async def test_missing_credentials(self, monkeypatch):
        await self._creds(monkeypatch, META_ACCESS_TOKEN="")
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="META_ACCESS_TOKEN"):
                await fetch_meta(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_http_error(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.get(url__regex=r"https://graph\.facebook\.com/.*/insights").mock(
            return_value=httpx.Response(400, text="bad token")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="Meta insights failed"):
                await fetch_meta(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )


class TestLinkedIn:
    async def _creds(self, monkeypatch, **overrides):
        values = {
            "LINKEDIN_ACCESS_TOKEN": "li-token",
            "LINKEDIN_AD_ACCOUNT_ID": "555",
        }
        values.update(overrides)
        _creds(monkeypatch, **values)

    @respx.mock
    async def test_success(self, monkeypatch):
        await self._creds(monkeypatch)
        route = respx.get(LINKEDIN_ANALYTICS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "dateRange": {"start": {"year": 2026, "month": 1, "day": 2}},
                            "impressions": 300,
                            "clicks": 30,
                            "costInLocalCurrency": "15.0",
                            "externalWebsiteConversions": 3,
                            "conversionValueInLocalCurrency": "60.0",
                        }
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as client:
            snapshots = await fetch_linkedin(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="777",
                start=START,
                end=END,
            )
        assert len(snapshots) == 1
        assert snapshots[0].platform_date == date(2026, 1, 2)
        assert snapshots[0].source == "linkedin_ads"
        assert route.calls[0].request.headers["LinkedIn-Version"] == "202411"

    async def test_missing_credentials(self, monkeypatch):
        await self._creds(monkeypatch, LINKEDIN_ACCESS_TOKEN="")
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="LINKEDIN_ACCESS_TOKEN"):
                await fetch_linkedin(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_http_error(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.get(LINKEDIN_ANALYTICS_URL).mock(return_value=httpx.Response(500, text="down"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="adAnalytics failed"):
                await fetch_linkedin(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )


class TestTikTok:
    async def _creds(self, monkeypatch, **overrides):
        values = {
            "TIKTOK_ACCESS_TOKEN": "tt-token",
            "TIKTOK_ADVERTISER_ID": "adv-1",
        }
        values.update(overrides)
        _creds(monkeypatch, **values)

    @respx.mock
    async def test_success(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.get(url__regex=r"https://business-api\.tiktok\.com/.*/report/integrated/get/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "dimensions": {
                                    "campaign_id": "888",
                                    "stat_time_day": "2026-01-03",
                                },
                                "metrics": {
                                    "impressions": "500",
                                    "clicks": "25",
                                    "spend": "10.0",
                                    "conversions": "2",
                                    "complete_payment": "45.0",
                                    "app_install": "1",
                                },
                            }
                        ]
                    },
                },
            )
        )
        async with httpx.AsyncClient() as client:
            snapshots = await fetch_tiktok(
                client,
                campaign_id=CAMPAIGN_ID,
                external_id="888",
                start=START,
                end=END,
            )
        assert len(snapshots) == 1
        assert snapshots[0].channel == "tiktok"
        assert snapshots[0].downloads == 1
        assert snapshots[0].revenue_usd == 45.0

    async def test_missing_credentials(self, monkeypatch):
        await self._creds(monkeypatch, TIKTOK_ACCESS_TOKEN="")
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="TIKTOK_ACCESS_TOKEN"):
                await fetch_tiktok(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_api_error_code(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.get(url__regex=r"https://business-api\.tiktok\.com/.*").mock(
            return_value=httpx.Response(200, json={"code": 40001, "message": "invalid advertiser"})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="invalid advertiser"):
                await fetch_tiktok(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )

    @respx.mock
    async def test_http_error(self, monkeypatch):
        await self._creds(monkeypatch)
        respx.get(url__regex=r"https://business-api\.tiktok\.com/.*").mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdPlatformError, match="TikTok report failed"):
                await fetch_tiktok(
                    client,
                    campaign_id=CAMPAIGN_ID,
                    external_id="1",
                    start=START,
                    end=END,
                )


class TestDispatchLive:
    async def test_dispatches_each_channel(self, monkeypatch):
        # Exercise real per-channel dispatch, not the dev/test mock shortcut.
        _creds(
            monkeypatch,
            GOOGLE_ADS_USE_MOCK=False,
            META_USE_MOCK=False,
            LINKEDIN_USE_MOCK=False,
            TIKTOK_USE_MOCK=False,
        )
        called = []

        def _fake(name):
            async def _fetch(client, *, campaign_id, external_id, start, end):
                called.append(name)
                return []

            return _fetch

        for channel in ("google", "meta", "linkedin", "tiktok"):
            monkeypatch.setattr(fetcher, f"fetch_{channel}", _fake(channel))

        for channel in ("google", "meta", "linkedin", "tiktok"):
            rows = await fetch_channel_snapshots(
                _scope(),
                channel,
                CAMPAIGN_ID,
                external_id="1",
                start=START,
                end=END,
            )
            assert rows == []

        assert called == ["google", "meta", "linkedin", "tiktok"]

    async def test_persist_empty_is_noop(self):
        assert await persist_snapshots(_scope(), CAMPAIGN_ID, []) == 0
