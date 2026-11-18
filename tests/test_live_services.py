"""Live-service tests.

These are opt-in (``ADFORGE_LIVE_TESTS=1``) because they hit real external
services. Ad-platform tests additionally skip unless the corresponding
credentials *and* a test campaign id are configured, so the suite is safe to
run on a laptop without ad accounts.

Run:
    $env:ADFORGE_LIVE_TESTS=1
    .venv\\Scripts\\python.exe -m pytest -m live -q
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from uuid import uuid4

import pytest

from adforge.agents.kpi_analytics.fetcher import fetch_channel_snapshots
from adforge.config import get_settings
from adforge.guardrails.data_isolation import TenantScope
from adforge.guardrails.exceptions import LLMTransportError
from adforge.llm.opencode_provider import OpencodeProvider

pytestmark = pytest.mark.live

LIVE_ENABLED = os.getenv("ADFORGE_LIVE_TESTS") == "1"
skip_live = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="set ADFORGE_LIVE_TESTS=1 to run live-service tests",
)


@skip_live
class TestOpencodeLive:
    async def test_chat_completion_round_trip(self):
        settings = get_settings()
        if not (settings.llm_base_url and settings.llm_model):
            pytest.skip("LLM_BASE_URL / LLM_MODEL are not configured")

        provider = OpencodeProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key or "public",
            timeout=120,
        )
        try:
            response = await provider.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a health check. Follow instructions exactly.",
                    },
                    {"role": "user", "content": "Reply with the single word OK."},
                ],
                temperature=0.0,
                # Reasoning models may spend the entire budget on hidden
                # reasoning before emitting the visible answer.
                max_tokens=256,
            )
        except LLMTransportError as exc:
            message = str(exc).lower()
            if "429" in message:
                pytest.skip("opencode free-tier quota is currently exhausted")
            raise
        assert response.content.strip().upper().startswith("OK")
        assert response.usage.total_tokens > 0


def _live_channel_config(channel: str) -> tuple[bool, str | None]:
    """Return ``(configured, test_campaign_external_id)`` for a channel."""
    s = get_settings()
    mapping = {
        "google": (
            all(
                [
                    s.google_ads_developer_token,
                    s.google_ads_client_id,
                    s.google_ads_client_secret,
                    s.google_ads_refresh_token,
                    s.google_ads_customer_id,
                ]
            ),
            os.getenv("GOOGLE_ADS_TEST_CAMPAIGN_ID"),
        ),
        "meta": (
            bool(s.meta_access_token and s.meta_ad_account_id),
            os.getenv("META_TEST_CAMPAIGN_ID"),
        ),
        "linkedin": (
            bool(s.linkedin_access_token and s.linkedin_ad_account_id),
            os.getenv("LINKEDIN_TEST_CAMPAIGN_ID"),
        ),
        "tiktok": (
            bool(s.tiktok_access_token and s.tiktok_advertiser_id),
            os.getenv("TIKTOK_TEST_CAMPAIGN_ID"),
        ),
    }
    return mapping[channel]


@skip_live
@pytest.mark.parametrize("channel", ["google", "meta", "linkedin", "tiktok"])
async def test_live_ad_platform_daily_snapshots(channel):
    configured, external_id = _live_channel_config(channel)
    if not configured:
        pytest.skip(f"{channel}: live credentials are not configured")
    if not external_id:
        pytest.skip(f"{channel}: <CHANNEL>_TEST_CAMPAIGN_ID is not set")

    today = date.today()
    snapshots = await fetch_channel_snapshots(
        TenantScope(tenant_id="live-test", campaign_id=uuid4()),
        channel,
        uuid4(),
        external_id=external_id,
        start=today - timedelta(days=2),
        end=today,
    )
    assert snapshots, f"{channel} returned no rows for campaign {external_id}"
    assert all(s.channel == channel for s in snapshots)
    assert all(not s.is_mock for s in snapshots)
    assert all(s.spend_usd >= 0 and s.impressions >= 0 for s in snapshots)
