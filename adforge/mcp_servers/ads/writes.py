"""Ad-platform write gateway.

AdForge deliberately does not invent campaign ids or pretend a write
succeeded. Publishing is delegated to a configured, authenticated webhook that
performs the platform-specific API calls. Operators can point that webhook at
their own Google/Meta/TikTok/LinkedIn adapter (or a serverless function).

Writes are disabled by default and require:
  1. ``ADFORGE_ENABLE_LIVE_WRITES=1``
  2. a valid single-use HITL approval token (enforced by the MCP decorator)
  3. ``ADFORGE_PUBLISH_WEBHOOK_URL``
  4. a budget within ``GUARDRAILS_MAX_CAMPAIGN_BUDGET_USD``
"""
from __future__ import annotations

import math
from typing import Any

import httpx

from adforge.config import get_settings


class WriteGatewayError(RuntimeError):
    """A write could not be performed safely."""


async def _call_webhook(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.adforge_enable_live_writes:
        raise WriteGatewayError(
            "live ad writes are disabled; set ADFORGE_ENABLE_LIVE_WRITES=1 "
            "only after configuring an approved publishing adapter"
        )
    if not settings.adforge_publish_webhook_url:
        raise WriteGatewayError(
            "ADFORGE_PUBLISH_WEBHOOK_URL is not configured; no campaign was "
            "published"
        )

    headers = {"Content-Type": "application/json"}
    if settings.adforge_publish_webhook_token:
        headers["Authorization"] = f"Bearer {settings.adforge_publish_webhook_token}"

    async with httpx.AsyncClient(
        timeout=settings.external_http_timeout, trust_env=False
    ) as client:
        response = await client.post(
            settings.adforge_publish_webhook_url,
            json={"action": action, **payload},
            headers=headers,
        )
    if response.status_code >= 400:
        raise WriteGatewayError(
            f"publish adapter HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise WriteGatewayError("publish adapter returned invalid JSON") from exc
    if not isinstance(body, dict) or body.get("error"):
        raise WriteGatewayError(
            f"publish adapter rejected the write: {str(body)[:300]}"
        )
    return body


def _validate_budget(budget_usd: float) -> None:
    settings = get_settings()
    if not isinstance(budget_usd, (int, float)) or not math.isfinite(budget_usd):
        raise WriteGatewayError("budget must be a finite number")
    if budget_usd <= 0:
        raise WriteGatewayError("budget must be positive")
    if budget_usd > settings.guardrails_max_campaign_budget_usd:
        raise WriteGatewayError(
            f"budget ${budget_usd:,.2f} exceeds the configured cap of "
            f"${settings.guardrails_max_campaign_budget_usd:,.2f}"
        )


async def publish_campaign(
    *,
    channel: str,
    campaign_name: str,
    budget_usd: float,
    creative: dict[str, Any],
    audience: dict[str, Any],
) -> dict[str, Any]:
    _validate_budget(budget_usd)
    body = await _call_webhook(
        "publish_campaign",
        {
            "channel": channel,
            "campaign_name": campaign_name,
            "budget_usd": budget_usd,
            "creative": creative,
            "audience": audience,
        },
    )
    external_id = body.get("external_campaign_id")
    if not external_id:
        raise WriteGatewayError(
            "publish adapter did not return external_campaign_id; refusing to "
            "report success"
        )
    return {
        "channel": channel,
        "external_campaign_id": str(external_id),
        "status": body.get("status", "unknown"),
        "budget_usd": budget_usd,
        "published_with_approval": True,
    }


async def pause_campaign(
    *, channel: str, external_campaign_id: str
) -> dict[str, Any]:
    body = await _call_webhook(
        "pause_campaign",
        {"channel": channel, "external_campaign_id": external_campaign_id},
    )
    return {
        "channel": channel,
        "external_campaign_id": external_campaign_id,
        "status": body.get("status", "unknown"),
        "paused_with_approval": True,
    }


async def update_budget(
    *, channel: str, external_campaign_id: str, new_budget_usd: float
) -> dict[str, Any]:
    _validate_budget(new_budget_usd)
    body = await _call_webhook(
        "update_budget",
        {
            "channel": channel,
            "external_campaign_id": external_campaign_id,
            "new_budget_usd": new_budget_usd,
        },
    )
    return {
        "channel": channel,
        "external_campaign_id": external_campaign_id,
        "daily_budget_usd": new_budget_usd,
        "status": body.get("status", "unknown"),
        "updated_with_approval": True,
    }
