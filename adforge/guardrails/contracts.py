"""Typed contracts for every agent's input and output.

Every agent in the graph MUST consume ``AgentInput`` and produce ``AgentOutput``.
Domain-specific payloads (CampaignBrief, MediaPlan, KpiSnapshot, ...) are the
schemas that agents pass through the LangGraph state. Enforcing types here means
a broken agent cannot silently corrupt downstream state.

The pattern:
    AgentInput  — validated, tenant-scoped request envelope
    AgentOutput — validated response envelope with a typed payload field
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Domain payloads (one per agent domain)
# --------------------------------------------------------------------------- #


class CampaignBrief(BaseModel):
    """Initial campaign brief — the user's input to the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(..., min_length=1, max_length=200)
    product_description: str = Field(..., min_length=10, max_length=2000)
    target_audience: str = Field(..., min_length=5, max_length=500)
    budget_usd: float = Field(..., gt=0, le=10_000_000)
    duration_days: int = Field(..., ge=1, le=365)
    business_objective: Literal[
        "awareness", "consideration", "conversion", "retention"
    ] = "conversion"
    channels_hint: list[Literal["google", "meta", "linkedin", "tiktok"]] = Field(
        default_factory=lambda: ["google", "meta"]
    )
    constraints: list[str] = Field(default_factory=list)

    @field_validator("channels_hint")
    @classmethod
    def _channels_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("channels_hint must not contain duplicates")
        return v

    @model_validator(mode="after")
    def _budget_within_cap(self) -> CampaignBrief:
        # Imported lazily to avoid a module-level config dependency in the
        # domain contracts (config itself must stay import-light).
        from adforge.config import get_settings

        cap = get_settings().guardrails_max_campaign_budget_usd
        if self.budget_usd > cap:
            raise ValueError(
                f"budget_usd ${self.budget_usd:,.2f} exceeds the configured "
                f"campaign cap of ${cap:,.2f}"
            )
        return self


class ResearchFindings(BaseModel):
    """Research agent output — market, competitors, keywords."""

    model_config = ConfigDict(extra="forbid")

    market_summary: str = Field(..., min_length=10)
    top_keywords: list[str] = Field(default_factory=list, max_length=50)
    competitor_creatives: list[dict[str, str]] = Field(default_factory=list)
    audience_persona: dict[str, Any] = Field(default_factory=dict)
    trends: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, max_length=20)


class CreativeConcept(BaseModel):
    """Creative Strategist output — ad copy + visual direction."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(..., min_length=3, max_length=120)
    primary_text: str = Field(..., min_length=10, max_length=500)
    cta: str = Field(..., min_length=2, max_length=30)
    visual_direction: str = Field(..., min_length=10, max_length=1000)
    ab_variants: list[dict[str, str]] = Field(default_factory=list, max_length=5)
    channel_adaptations: dict[str, dict[str, str]] = Field(default_factory=dict)


class MediaPlan(BaseModel):
    """Media Planner output — channel mix, budget split, bidding."""

    model_config = ConfigDict(extra="forbid")

    channel_allocations: list[dict[str, Any]] = Field(..., min_length=1)
    total_budget_usd: float = Field(..., gt=0)
    bidding_strategy: Literal[
        "max_clicks", "max_conversions", "target_cpa", "target_roas", "manual_cpc"
    ] = "max_conversions"
    audience_targeting: dict[str, Any]
    expected_reach: int = Field(..., ge=0)
    expected_impressions: int = Field(..., ge=0)
    flight_dates: dict[str, str]

    @field_validator("channel_allocations")
    @classmethod
    def _allocations_valid(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not v:
            raise ValueError("at least one channel allocation required")
        seen: set[str] = set()
        for a in v:
            if "channel" not in a or "budget_usd" not in a:
                raise ValueError("each allocation needs 'channel' and 'budget_usd'")
            channel = str(a["channel"]).strip()
            if not channel:
                raise ValueError("allocation channel must be non-empty")
            if channel.lower() in seen:
                raise ValueError(f"duplicate channel allocation: {channel!r}")
            seen.add(channel.lower())
            budget = a["budget_usd"]
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not math.isfinite(float(budget))
            ):
                raise ValueError("budget must be a finite number")
            if budget <= 0:
                raise ValueError("budget must be positive")
        return v

    @model_validator(mode="after")
    def _allocation_total_bounded(self) -> MediaPlan:
        from adforge.config import get_settings

        total = self.total_budget_usd
        if not math.isfinite(total):
            raise ValueError("total_budget_usd must be finite")
        cap = get_settings().guardrails_max_campaign_budget_usd
        if total > cap:
            raise ValueError(
                f"total_budget_usd ${total:,.2f} exceeds the configured "
                f"campaign cap of ${cap:,.2f}"
            )
        allocated = sum(float(a["budget_usd"]) for a in self.channel_allocations)
        if allocated > total + 0.01:
            raise ValueError(
                f"channel allocations total ${allocated:,.2f}, which exceeds "
                f"total_budget_usd ${total:,.2f}"
            )
        return self


class KpiSnapshot(BaseModel):
    """KPI/Analytics agent output — one snapshot per channel per timestamp."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["google", "meta", "linkedin", "tiktok"]
    campaign_external_id: str | None = None
    impressions: int = Field(..., ge=0)
    clicks: int = Field(..., ge=0)
    spend_usd: float = Field(..., ge=0)
    conversions: int = Field(..., ge=0)
    revenue_usd: float = Field(..., ge=0)
    ctr: float = Field(..., ge=0)
    cvr: float = Field(..., ge=0)
    cpc: float = Field(..., ge=0)
    cpa: float = Field(..., ge=0)
    cpm: float = Field(..., ge=0)
    roas: float = Field(..., ge=0)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Conversion events that are app installs / downloads, when the platform
    # exposes them separately from generic conversions.
    downloads: int = Field(default=0, ge=0)
    # Provenance: "google_ads" | "meta_ads" | "linkedin_ads" | "tiktok_ads".
    source: str = "unknown"
    # True only for seeded/dev fixtures; real platform rows must leave this False.
    is_mock: bool = False
    # Daily grain used for idempotent upserts.
    platform_date: date | None = None

    @field_validator("ctr", "cvr")
    @classmethod
    def _ratio_bounded(cls, v: float) -> float:
        if not 0 <= v <= 1.0:
            raise ValueError("ctr/cvr must be 0..1")
        return v


class RoiReport(BaseModel):
    """ROI Calculator output — answers 'was the campaign worth it?'."""

    model_config = ConfigDict(extra="forbid")

    total_spend_usd: float = Field(..., ge=0)
    total_revenue_usd: float = Field(..., ge=0)
    total_conversions: int = Field(..., ge=0)
    blended_roas: float = Field(..., ge=0)
    blended_cpa: float = Field(..., ge=0)
    payback_period_days: float | None = None
    lifetime_value_usd: float | None = None
    net_profit_usd: float
    recommendation: Literal["scale", "optimize", "pause", "kill"]
    rationale: str = Field(..., min_length=20)
    per_channel_breakdown: list[dict[str, Any]] = Field(default_factory=list)


class ComplianceVerdict(BaseModel):
    """Compliance agent output — gate before any creative goes live."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    flagged_issues: list[str] = Field(default_factory=list)
    required_disclosures: list[str] = Field(default_factory=list)
    reviewer_notes: str = ""
    blocked_terms: list[str] = Field(default_factory=list)


class OptimizationSuggestion(BaseModel):
    """Optimization Loop output — reallocates budget based on live KPIs."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["google", "meta", "linkedin", "tiktok"]
    action: Literal["increase_budget", "decrease_budget", "pause", "scale", "rotate_creative"]
    delta_pct: float = Field(..., ge=-1.0, le=2.0)
    rationale: str = Field(..., min_length=10)
    expected_impact: dict[str, Any]


# --------------------------------------------------------------------------- #
# Agent envelopes — universal input / output contract
# --------------------------------------------------------------------------- #


Payload = (
    CampaignBrief
    | ResearchFindings
    | CreativeConcept
    | MediaPlan
    | KpiSnapshot
    | RoiReport
    | ComplianceVerdict
    | OptimizationSuggestion
    | dict[str, Any]
)


class AgentInput(BaseModel):
    """Validated, tenant-scoped request envelope consumed by every agent.

    Every agent node in the LangGraph orchestrator MUST receive this. The
    ``tenant_id`` field is the linchpin of data isolation — see
    ``guardrails.data_isolation``.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1)
    campaign_id: UUID
    agent_name: str = Field(..., min_length=1)
    brief: CampaignBrief | None = None
    upstream_payloads: dict[str, Any] = Field(default_factory=dict)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentOutput(BaseModel):
    """Validated response envelope produced by every agent."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    tenant_id: str
    campaign_id: UUID
    agent_name: str
    payload: dict[str, Any]
    usage: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    approved_by_compliance: bool | None = None
    error: str | None = None
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
