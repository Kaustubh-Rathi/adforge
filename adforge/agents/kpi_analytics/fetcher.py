"""KPI fetcher — real ad-platform HTTP clients.

Each platform exposes daily campaign metrics through a different REST API:

    Google Ads   googleAds:searchStream (GAQL) + OAuth2 refresh
    Meta Ads     Graph API /insights (campaign level)
    LinkedIn     /rest/adAnalytics
    TikTok       /open_api/<v>/report/integrated/get/

Every path normalises into :class:`KpiSnapshot` and carries a
``platform_date`` so persistence is an idempotent daily upsert.

There is no simulated data path in staging/prod. Missing credentials, missing
external campaign ids, or bad responses raise :class:`AdPlatformError`, which
the KPI agent records as an error. Tests exercise these paths with recorded
HTTP fixtures through ``respx``.

Exception: in ``dev``/``test`` environments, when a channel's ``*_USE_MOCK``
setting is enabled, :func:`fetch_channel_snapshots` short-circuits all
networking and returns deterministic mock snapshots (``is_mock=true``,
``source="mock_<channel>"``) so zero-credential demos can exercise the ROI and
optimization agents end to end.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx

from adforge.config import get_settings
from adforge.guardrails.contracts import Channel, KpiSnapshot
from adforge.guardrails.data_isolation import TenantScope
from adforge.guardrails.rate_limiter import get_rate_limiter

logger = logging.getLogger("adforge.kpi")

_GOOGLE_TOKEN_SAFETY_SECONDS = 60
_google_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


class AdPlatformError(RuntimeError):
    """A configured ad platform returned an error or unusable payload."""


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(value[: len(fmt) + 2].strip(), fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(min(numerator / denominator, 1.0), 4)


def build_snapshot(
    *,
    channel: Channel,
    campaign_id: UUID,
    campaign_external_id: str | None,
    platform_date: date,
    impressions: int,
    clicks: int,
    spend_usd: float,
    conversions: int,
    revenue_usd: float,
    downloads: int = 0,
    source: str,
    is_mock: bool = False,
) -> KpiSnapshot:
    """Build a validated snapshot with derived metrics computed consistently."""
    impressions = max(impressions, 0)
    clicks = max(clicks, 0)
    spend_usd = max(spend_usd, 0.0)
    conversions = max(conversions, 0)
    revenue_usd = max(revenue_usd, 0.0)
    captured = datetime.combine(platform_date, datetime.min.time(), tzinfo=UTC)
    return KpiSnapshot(
        channel=channel,
        campaign_external_id=campaign_external_id,
        impressions=impressions,
        clicks=clicks,
        spend_usd=round(spend_usd, 2),
        conversions=conversions,
        revenue_usd=round(revenue_usd, 2),
        ctr=_ratio(clicks, impressions),
        cvr=_ratio(conversions, clicks),
        cpc=round(spend_usd / clicks, 2) if clicks else 0.0,
        cpa=round(spend_usd / conversions, 2) if conversions else 0.0,
        cpm=round(spend_usd / impressions * 1000, 2) if impressions else 0.0,
        roas=round(revenue_usd / spend_usd, 2) if spend_usd else 0.0,
        captured_at=captured,
        downloads=max(downloads, 0),
        source=source,
        is_mock=is_mock,
        platform_date=platform_date,
    )


# --------------------------------------------------------------------------- #
# Google Ads
# --------------------------------------------------------------------------- #


async def _google_access_token(client: httpx.AsyncClient) -> str:
    s = get_settings()
    now = time.time()
    cached = _google_token_cache.get("access_token")
    if cached and _google_token_cache.get("expires_at", 0) > now + _GOOGLE_TOKEN_SAFETY_SECONDS:
        return str(cached)

    response = await client.post(
        s.google_oauth_token_url,
        data={
            "client_id": s.google_ads_client_id,
            "client_secret": s.google_ads_client_secret,
            "refresh_token": s.google_ads_refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if response.status_code >= 400:
        raise AdPlatformError(
            f"Google OAuth token refresh failed: HTTP {response.status_code} {response.text[:300]}"
        )
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise AdPlatformError("Google OAuth response had no access_token")
    _google_token_cache["access_token"] = token
    _google_token_cache["expires_at"] = now + _as_float(body.get("expires_in"), 3600)
    return str(token)


async def fetch_google(
    client: httpx.AsyncClient,
    *,
    campaign_id: UUID,
    external_id: str,
    start: date,
    end: date,
) -> list[KpiSnapshot]:
    s = get_settings()
    required = [
        s.google_ads_developer_token,
        s.google_ads_client_id,
        s.google_ads_client_secret,
        s.google_ads_refresh_token,
        s.google_ads_customer_id,
    ]
    if not all(required):
        raise AdPlatformError(
            "Google Ads live mode requires GOOGLE_ADS_DEVELOPER_TOKEN, "
            "GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET, "
            "GOOGLE_ADS_REFRESH_TOKEN and GOOGLE_ADS_CUSTOMER_ID"
        )

    token = await _google_access_token(client)
    customer_id = s.google_ads_customer_id.replace("-", "")
    query = (
        "SELECT campaign.id, segments.date, metrics.impressions, metrics.clicks, "
        "metrics.cost_micros, metrics.conversions, metrics.conversions_value "
        "FROM campaign "
        f"WHERE campaign.id = {int(external_id)} "
        f"AND segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )
    response = await client.post(
        f"{s.google_ads_api_base.rstrip('/')}/{s.google_ads_api_version}/"
        f"customers/{customer_id}/googleAds:searchStream",
        headers={
            "Authorization": f"Bearer {token}",
            "developer-token": s.google_ads_developer_token,
            "Content-Type": "application/json",
        },
        json={"query": query},
    )
    if response.status_code >= 400:
        raise AdPlatformError(
            f"Google Ads searchStream failed: HTTP {response.status_code} {response.text[:300]}"
        )

    payload = response.json()
    chunks = payload if isinstance(payload, list) else [payload]
    snapshots: list[KpiSnapshot] = []
    for chunk in chunks:
        for result in chunk.get("results", []) if isinstance(chunk, dict) else []:
            metrics = result.get("metrics", {})
            segment_date = _as_date(result.get("segments", {}).get("date"))
            if not segment_date:
                continue
            snapshots.append(
                build_snapshot(
                    channel="google",
                    campaign_id=campaign_id,
                    campaign_external_id=external_id,
                    platform_date=segment_date,
                    impressions=_as_int(metrics.get("impressions")),
                    clicks=_as_int(metrics.get("clicks")),
                    spend_usd=_as_float(metrics.get("costMicros")) / 1_000_000,
                    conversions=_as_int(metrics.get("conversions")),
                    revenue_usd=_as_float(metrics.get("conversionsValue")),
                    source="google_ads",
                )
            )
    return snapshots


# --------------------------------------------------------------------------- #
# Meta Ads
# --------------------------------------------------------------------------- #


_META_CONVERSION_ACTIONS = {
    "purchase",
    "offsite_conversion.fb_pixel_purchase",
    "lead",
    "offsite_conversion.fb_pixel_lead",
    "complete_registration",
    "offsite_conversion.fb_pixel_complete_registration",
    "subscribe",
    "offsite_conversion.fb_pixel_subscribe",
}


async def fetch_meta(
    client: httpx.AsyncClient,
    *,
    campaign_id: UUID,
    external_id: str,
    start: date,
    end: date,
) -> list[KpiSnapshot]:
    s = get_settings()
    if not (s.meta_access_token and s.meta_ad_account_id):
        raise AdPlatformError(
            "Meta Ads live mode requires META_ACCESS_TOKEN and META_AD_ACCOUNT_ID"
        )

    account = s.meta_ad_account_id.removeprefix("act_")
    response = await client.get(
        f"{s.meta_graph_base.rstrip('/')}/{s.meta_graph_api_version}/act_{account}/insights",
        params={
            "level": "campaign",
            "fields": ("campaign_id,campaign_name,impressions,clicks,spend,actions,action_values"),
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
            "time_increment": "1",
            "limit": 500,
            "access_token": s.meta_access_token,
        },
    )
    if response.status_code >= 400:
        raise AdPlatformError(
            f"Meta insights failed: HTTP {response.status_code} {response.text[:300]}"
        )

    snapshots: list[KpiSnapshot] = []
    for row in response.json().get("data", []):
        if str(row.get("campaign_id")) != str(external_id):
            continue
        day = _as_date(row.get("date_start"))
        if not day:
            continue
        conversions = sum(
            _as_int(a.get("value"))
            for a in row.get("actions", [])
            if a.get("action_type") in _META_CONVERSION_ACTIONS
        )
        revenue = sum(
            _as_float(a.get("value"))
            for a in row.get("action_values", [])
            if a.get("action_type") in _META_CONVERSION_ACTIONS
        )
        snapshots.append(
            build_snapshot(
                channel="meta",
                campaign_id=campaign_id,
                campaign_external_id=external_id,
                platform_date=day,
                impressions=_as_int(row.get("impressions")),
                clicks=_as_int(row.get("clicks")),
                spend_usd=_as_float(row.get("spend")),
                conversions=conversions,
                revenue_usd=revenue,
                source="meta_ads",
            )
        )
    return snapshots


# --------------------------------------------------------------------------- #
# LinkedIn Ads
# --------------------------------------------------------------------------- #


async def fetch_linkedin(
    client: httpx.AsyncClient,
    *,
    campaign_id: UUID,
    external_id: str,
    start: date,
    end: date,
    ad_account_id: str | None = None,
) -> list[KpiSnapshot]:
    s = get_settings()
    account_id = ad_account_id or s.linkedin_ad_account_id
    if not (s.linkedin_access_token and account_id):
        raise AdPlatformError(
            "LinkedIn Ads live mode requires LINKEDIN_ACCESS_TOKEN and LINKEDIN_AD_ACCOUNT_ID"
        )

    def _tuple(d: date) -> str:
        return f"(year:{d.year},month:{d.month},day:{d.day})"

    response = await client.get(
        f"{s.linkedin_api_base.rstrip('/')}/rest/adAnalytics",
        headers={
            "Authorization": f"Bearer {s.linkedin_access_token}",
            "LinkedIn-Version": s.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        params={
            "q": "analytics",
            "dateRange": f"(start:{_tuple(start)},end:{_tuple(end)})",
            "timeGranularity": "DAILY",
            "accounts": f"List(urn:li:sponsoredAccount:{account_id})",
            "campaigns": f"List(urn:li:sponsoredCampaign:{external_id})",
            "fields": (
                "dateRange,impressions,clicks,costInLocalCurrency,"
                "externalWebsiteConversions,conversionValueInLocalCurrency"
            ),
        },
    )
    if response.status_code >= 400:
        raise AdPlatformError(
            f"LinkedIn adAnalytics failed: HTTP {response.status_code} {response.text[:300]}"
        )

    snapshots: list[KpiSnapshot] = []
    for row in response.json().get("elements", []):
        start_obj = (row.get("dateRange") or {}).get("start") or {}
        day = date(
            _as_int(start_obj.get("year"), 1970),
            max(1, _as_int(start_obj.get("month"), 1)),
            max(1, _as_int(start_obj.get("day"), 1)),
        )
        snapshots.append(
            build_snapshot(
                channel="linkedin",
                campaign_id=campaign_id,
                campaign_external_id=external_id,
                platform_date=day,
                impressions=_as_int(row.get("impressions")),
                clicks=_as_int(row.get("clicks")),
                spend_usd=_as_float(row.get("costInLocalCurrency")),
                conversions=_as_int(row.get("externalWebsiteConversions")),
                revenue_usd=_as_float(row.get("conversionValueInLocalCurrency")),
                source="linkedin_ads",
            )
        )
    return snapshots


# --------------------------------------------------------------------------- #
# TikTok Ads
# --------------------------------------------------------------------------- #


async def fetch_tiktok(
    client: httpx.AsyncClient,
    *,
    campaign_id: UUID,
    external_id: str,
    start: date,
    end: date,
) -> list[KpiSnapshot]:
    s = get_settings()
    if not (s.tiktok_access_token and s.tiktok_advertiser_id):
        raise AdPlatformError(
            "TikTok live mode requires TIKTOK_ACCESS_TOKEN and TIKTOK_ADVERTISER_ID"
        )

    response = await client.get(
        f"{s.tiktok_api_base.rstrip('/')}/open_api/{s.tiktok_api_version}/report/integrated/get/",
        headers={"Access-Token": s.tiktok_access_token},
        params={
            "advertiser_id": s.tiktok_advertiser_id,
            "report_type": "BASIC",
            "data_level": "AUCTION_CAMPAIGN",
            "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
            "metrics": json.dumps(
                [
                    "impressions",
                    "clicks",
                    "spend",
                    "conversions",
                    "total_complete_payment_rate",
                    "conversion_rate",
                    "complete_payment",
                ]
            ),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "filtering": json.dumps(
                [
                    {
                        "field_name": "campaign_ids",
                        "filter_type": "IN",
                        "filter_value": json.dumps([str(external_id)]),
                    }
                ]
            ),
        },
    )
    if response.status_code >= 400:
        raise AdPlatformError(
            f"TikTok report failed: HTTP {response.status_code} {response.text[:300]}"
        )
    body = response.json()
    if body.get("code") not in (0, None):
        raise AdPlatformError(f"TikTok report error: {body.get('message')}")

    snapshots: list[KpiSnapshot] = []
    for row in (body.get("data") or {}).get("list", []):
        dims = row.get("dimensions", {})
        metrics = row.get("metrics", {})
        day = _as_date(dims.get("stat_time_day"))
        if not day:
            continue
        snapshots.append(
            build_snapshot(
                channel="tiktok",
                campaign_id=campaign_id,
                campaign_external_id=str(dims.get("campaign_id") or external_id),
                platform_date=day,
                impressions=_as_int(metrics.get("impressions")),
                clicks=_as_int(metrics.get("clicks")),
                spend_usd=_as_float(metrics.get("spend")),
                conversions=_as_int(metrics.get("conversions")),
                revenue_usd=_as_float(metrics.get("complete_payment")),
                downloads=_as_int(metrics.get("app_install")),
                source="tiktok_ads",
            )
        )
    return snapshots


# --------------------------------------------------------------------------- #
# Deterministic mock path (dev/test only)
# --------------------------------------------------------------------------- #

_MOCK_DEFAULT_DAILY_BUDGET_USD = 1000.0
_MOCK_AVG_ORDER_VALUE_USD = 50.0


def _mock_rng(tenant_id: str, campaign_id: UUID, channel: str, day: date) -> random.Random:
    seed = int(
        hashlib.md5(f"{tenant_id}|{campaign_id}|{channel}|{day.isoformat()}".encode()).hexdigest(),
        16,
    )
    return random.Random(seed)


def _fetch_mock_snapshots(
    scope: TenantScope,
    channel: Channel,
    campaign_id: UUID,
    *,
    start: date,
    end: date,
    budget_usd: float | None = None,
) -> list[KpiSnapshot]:
    """Generate deterministic daily snapshots without touching the network.

    Metrics mirror the seeding style in ``adforge.storage.seed``. When a
    channel budget is supplied, daily spend is scaled so the window total
    stays under it. Every snapshot is labelled ``is_mock=True`` with a
    ``mock_<channel>`` source.
    """
    days = (end - start).days + 1
    daily_budget = (
        budget_usd / days if budget_usd and budget_usd > 0 else _MOCK_DEFAULT_DAILY_BUDGET_USD
    )
    external_id = f"mock-{channel}-{str(campaign_id)[:8]}"

    snapshots: list[KpiSnapshot] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        rng = _mock_rng(scope.tenant_id, campaign_id, channel, day)
        impressions = rng.randint(2000, 12000)
        clicks = int(impressions * rng.uniform(0.01, 0.05))
        spend_usd = round(daily_budget * rng.uniform(0.60, 0.95), 2)
        conversions = int(clicks * rng.uniform(0.03, 0.10))
        revenue_usd = conversions * _MOCK_AVG_ORDER_VALUE_USD
        snapshots.append(
            build_snapshot(
                channel=channel,
                campaign_id=campaign_id,
                campaign_external_id=external_id,
                platform_date=day,
                impressions=impressions,
                clicks=clicks,
                spend_usd=spend_usd,
                conversions=conversions,
                revenue_usd=revenue_usd,
                source=f"mock_{channel}",
                is_mock=True,
            )
        )
    return snapshots


# --------------------------------------------------------------------------- #
# Dispatch + persistence
# --------------------------------------------------------------------------- #


async def fetch_channel_snapshots(
    scope: TenantScope,
    channel: str,
    campaign_id: UUID,
    *,
    external_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
    days_back: int = 7,
    budget_usd: float | None = None,
) -> list[KpiSnapshot]:
    """Fetch daily snapshots for one channel from its real reporting API.

    In dev/test with the channel's ``*_use_mock`` setting enabled, returns
    deterministic mock snapshots instead — no rate limiter, no HTTP.
    """
    if channel not in ("google", "meta", "linkedin", "tiktok"):
        raise AdPlatformError(f"Unsupported ad channel: {channel}")

    # Narrowed by the guard above; the outer signature stays ``str`` so callers
    # get an explicit AdPlatformError for an unsupported channel rather than a
    # type error.
    validated_channel: Channel = cast("Channel", channel)

    settings = get_settings()
    today = datetime.now(UTC).date()
    end = end or today
    start = start or (end - timedelta(days=max(days_back - 1, 0)))
    if end < start:
        raise AdPlatformError("end date must not be before start date")

    if settings.app_env in ("dev", "test"):
        use_mock = {
            "google": settings.google_ads_use_mock,
            "meta": settings.meta_use_mock,
            "linkedin": settings.linkedin_use_mock,
            "tiktok": settings.tiktok_use_mock,
        }[validated_channel]
        if use_mock:
            return _fetch_mock_snapshots(
                scope,
                validated_channel,
                campaign_id,
                start=start,
                end=end,
                budget_usd=budget_usd,
            )

    if not external_id:
        raise AdPlatformError(
            f"{channel} has no campaign_external_id in the media plan allocation; "
            "publish the campaign first or supply the platform campaign id"
        )

    limiter = get_rate_limiter(channel)  # type: ignore[arg-type]
    await limiter.acquire(channel, tenant_id=scope.tenant_id, campaign_id=str(campaign_id))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        timeout=get_settings().external_http_timeout, trust_env=False
    ) as client:
        if channel == "google":
            return await fetch_google(
                client,
                campaign_id=campaign_id,
                external_id=external_id,
                start=start,
                end=end,
            )
        if channel == "meta":
            return await fetch_meta(
                client,
                campaign_id=campaign_id,
                external_id=external_id,
                start=start,
                end=end,
            )
        if channel == "linkedin":
            return await fetch_linkedin(
                client,
                campaign_id=campaign_id,
                external_id=external_id,
                start=start,
                end=end,
            )
        if channel == "tiktok":
            return await fetch_tiktok(
                client,
                campaign_id=campaign_id,
                external_id=external_id,
                start=start,
                end=end,
            )
    raise AdPlatformError(f"Unsupported ad channel: {channel}")


async def fetch_all_channels(
    scope: TenantScope, channel: str, campaign_id: UUID, **kwargs: Any
) -> KpiSnapshot | None:
    """Backwards-compatible single-snapshot helper (latest day)."""
    snapshots = await fetch_channel_snapshots(scope, channel, campaign_id, **kwargs)
    return snapshots[-1] if snapshots else None


async def persist_snapshots(
    scope: TenantScope, campaign_id: UUID, snapshots: list[KpiSnapshot]
) -> int:
    """Idempotently upsert daily snapshots + roll up campaign totals."""
    if not snapshots:
        return 0

    from sqlalchemy import text as sql_text

    from adforge.storage.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        for snap in snapshots:
            platform_date = snap.platform_date or snap.captured_at.date()
            await session.execute(
                sql_text(
                    "INSERT INTO kpi_snapshots "
                    "(tenant_id, campaign_id, channel, campaign_external_id, "
                    " impressions, clicks, spend_usd, conversions, revenue_usd, "
                    " ctr, cvr, cpc, cpa, cpm, roas, downloads, source, is_mock, "
                    " platform_date, captured_at) "
                    "VALUES (:tid, :cid, :ch, :ext, :imp, :clk, :spd, :cnv, :rev, "
                    "        :ctr, :cvr, :cpc, :cpa, :cpm, :roas, :dl, :src, :mock, "
                    "        :pdate, :cap) "
                    "ON CONFLICT (tenant_id, campaign_id, channel, platform_date) "
                    "DO UPDATE SET "
                    " campaign_external_id = EXCLUDED.campaign_external_id, "
                    " impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks, "
                    " spend_usd = EXCLUDED.spend_usd, conversions = EXCLUDED.conversions, "
                    " revenue_usd = EXCLUDED.revenue_usd, ctr = EXCLUDED.ctr, "
                    " cvr = EXCLUDED.cvr, cpc = EXCLUDED.cpc, cpa = EXCLUDED.cpa, "
                    " cpm = EXCLUDED.cpm, roas = EXCLUDED.roas, "
                    " downloads = EXCLUDED.downloads, source = EXCLUDED.source, "
                    " is_mock = EXCLUDED.is_mock, captured_at = EXCLUDED.captured_at"
                ),
                {
                    "tid": scope.tenant_id,
                    "cid": str(campaign_id),
                    "ch": snap.channel,
                    "ext": snap.campaign_external_id,
                    "imp": snap.impressions,
                    "clk": snap.clicks,
                    "spd": snap.spend_usd,
                    "cnv": snap.conversions,
                    "rev": snap.revenue_usd,
                    "ctr": snap.ctr,
                    "cvr": snap.cvr,
                    "cpc": snap.cpc,
                    "cpa": snap.cpa,
                    "cpm": snap.cpm,
                    "roas": snap.roas,
                    "dl": snap.downloads,
                    "src": snap.source,
                    "mock": snap.is_mock,
                    "pdate": platform_date,
                    "cap": snap.captured_at,
                },
            )
        await session.execute(
            sql_text(
                "UPDATE campaigns SET "
                " total_spend_usd = COALESCE((SELECT SUM(spend_usd) FROM kpi_snapshots "
                "   WHERE tenant_id = :tid AND campaign_id = :cid), 0), "
                " total_revenue_usd = COALESCE((SELECT SUM(revenue_usd) FROM kpi_snapshots "
                "   WHERE tenant_id = :tid AND campaign_id = :cid), 0), "
                " updated_at = now() "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {"tid": scope.tenant_id, "cid": str(campaign_id)},
        )
        await session.commit()
    return len(snapshots)
