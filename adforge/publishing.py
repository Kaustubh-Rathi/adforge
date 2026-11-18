"""Approval-gated campaign publishing.

This is the only path that can turn an ``approved`` campaign into a ``live``
one. It is orchestrator-only — the LLM never receives these tools.

Safety properties:
  * live writes must be explicitly enabled,
  * a publishing adapter (authenticated webhook) must be configured,
  * the single-use HITL token must match tenant + campaign + operation +
    reviewer and is consumed atomically before any external call,
  * every budget is checked against the configured cap.
"""
from __future__ import annotations

import math
from typing import Any

from adforge.config import get_settings
from adforge.mcp_servers.ads import writes
from adforge.mcp_servers.permissions import verify_and_consume_approval


class PublishError(RuntimeError):
    """The campaign could not be published safely."""


def _positive_finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise PublishError(f"{label} must be finite and positive, got {value!r}")
    return number


async def publish_approved_campaign(
    *,
    campaign_id: str,
    tenant_id: str,
    reviewer_email: str,
    approval_token: str,
    campaign_name: str,
    creative: dict[str, Any],
    media_plan: dict[str, Any],
    brief: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Publish every allocation in an approved media plan.

    Raises :class:`PublishError` before any external call if writes are
    disabled, the adapter is missing, the token is invalid/used, or the plan
    has no allocatable channels.
    """
    settings = get_settings()
    if not settings.adforge_enable_live_writes:
        raise PublishError(
            "live ad writes are disabled; set ADFORGE_ENABLE_LIVE_WRITES=1 "
            "only after configuring an approved publishing adapter"
        )
    if not settings.adforge_publish_webhook_url:
        raise PublishError(
            "ADFORGE_PUBLISH_WEBHOOK_URL is not configured; no campaign was "
            "published"
        )

    # Validate the whole money envelope before consuming the single-use token
    # or touching any external service.
    plan_total = _positive_finite(
        media_plan.get("total_budget_usd"), "media plan total_budget_usd"
    )
    if plan_total > settings.guardrails_max_campaign_budget_usd:
        raise PublishError(
            f"media plan budget ${plan_total:,.2f} exceeds the configured cap "
            f"of ${settings.guardrails_max_campaign_budget_usd:,.2f}"
        )

    brief_budget = (brief or {}).get("budget_usd")
    if brief_budget is not None:
        approved_budget = _positive_finite(
            brief_budget, "campaign brief budget_usd"
        )
        if plan_total > approved_budget + 0.01:
            raise PublishError(
                f"media plan budget ${plan_total:,.2f} exceeds the approved "
                f"campaign budget ${approved_budget:,.2f}"
            )

    allocations = [
        allocation
        for allocation in media_plan.get("channel_allocations", [])
        if allocation.get("channel")
    ]
    if not allocations:
        raise PublishError("media plan has no channel allocations to publish")

    seen_channels: set[str] = set()
    allocated_total = 0.0
    for allocation in allocations:
        channel = str(allocation["channel"]).strip()
        key = channel.lower()
        if not channel:
            raise PublishError("channel allocations must name a channel")
        if key in seen_channels:
            raise PublishError(f"duplicate channel allocation: {channel!r}")
        seen_channels.add(key)
        allocated_total += _positive_finite(
            allocation.get("budget_usd"),
            f"budget for channel {channel!r}",
        )
    if allocated_total > plan_total + 0.01:
        raise PublishError(
            f"channel allocations total ${allocated_total:,.2f}, which exceeds "
            f"the media plan budget ${plan_total:,.2f}"
        )

    claims = await verify_and_consume_approval(
        approval_token,
        campaign_id,
        "publish_campaign",
        tenant_id=tenant_id,
        reviewer_email=reviewer_email,
    )
    if claims is None:
        raise PublishError(
            "approval token is invalid, expired, already used, or not bound "
            "to this tenant/campaign/reviewer"
        )

    audience = media_plan.get("audience_targeting", {})
    results: list[dict[str, Any]] = []
    for allocation in allocations:
        channel = str(allocation["channel"]).strip()
        budget = float(allocation["budget_usd"])
        try:
            result = await writes.publish_campaign(
                channel=channel,
                campaign_name=campaign_name,
                budget_usd=budget,
                creative=creative,
                audience=audience,
            )
        except writes.WriteGatewayError as exc:
            raise PublishError(
                f"channel {channel!r} failed after {len(results)} successful "
                f"publish(es): {exc}"
            ) from exc
        results.append(result)
    return results
