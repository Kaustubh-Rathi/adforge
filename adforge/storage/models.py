"""ORM models — Campaign, AgentRun, KpiSnapshot, AuditEvent, CreativeArtifact."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, Integer, String, Text, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID as PgUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adforge.storage.base import Base, TenantMixin, TimestampMixin


class Campaign(Base, TenantMixin, TimestampMixin):
    """A campaign — one advertiser's end-to-end ad campaign.

    All other rows reference campaign_id + tenant_id. The orchestrator
    creates one of these per ad campaign brief.
    """

    __tablename__ = "campaigns"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    # draft -> researching -> designing -> compliant -> live -> measuring -> closed
    total_spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_revenue_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="campaign")
    kpi_snapshots: Mapped[list["KpiSnapshotRow"]] = relationship(back_populates="campaign")
    creatives: Mapped[list["CreativeArtifact"]] = relationship(back_populates="campaign")


class AgentRun(Base, TenantMixin, TimestampMixin):
    """One execution of one agent for one campaign.

    Stores input + output payload (as JSONB) + token usage + status.
    Used by the dashboard's 'Agent Traces' page and by the audit log.
    """

    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False, index=True
    )
    request_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by_compliance: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    # pending -> running -> succeeded -> failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="agent_runs")


class KpiSnapshotRow(Base, TenantMixin, TimestampMixin):
    """One KPI snapshot for one channel at one timestamp.

    The KPI/Analytics agent appends a row every time it polls the ad platforms.
    """

    __tablename__ = "kpi_snapshots"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    campaign_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    revenue_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    cvr: Mapped[float] = mapped_column(Float, default=0.0)
    cpc: Mapped[float] = mapped_column(Float, default=0.0)
    cpa: Mapped[float] = mapped_column(Float, default=0.0)
    cpm: Mapped[float] = mapped_column(Float, default=0.0)
    roas: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    campaign: Mapped[Campaign] = relationship(back_populates="kpi_snapshots")


class CreativeArtifact(Base, TenantMixin, TimestampMixin):
    """A piece of creative produced by the Creative agent — text, image concept, etc."""

    __tablename__ = "creative_artifacts"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(64), default="creative_strategist")
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    headline: Mapped[str] = mapped_column(String(200), nullable=False)
    primary_text: Mapped[str] = mapped_column(Text, nullable=False)
    cta: Mapped[str] = mapped_column(String(64), nullable=False)
    visual_direction: Mapped[str] = mapped_column(Text, nullable=False)
    ab_variants: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    approved_by_compliance: Mapped[bool] = mapped_column(Boolean, default=False)

    campaign: Mapped[Campaign] = relationship(back_populates="creatives")


class AuditEventRow(Base, TenantMixin):
    """Append-only audit log row — never UPDATEd, never DELETEd.

    Written by ``guardrails.audit_log`` via raw SQL (no ORM model methods
    that could allow UPDATE).
    """

    __tablename__ = "audit_events"

    event_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    campaign_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
