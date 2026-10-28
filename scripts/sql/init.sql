-- AdForge — initial Postgres schema (run by docker-compose entrypoint)
-- This is a fallback; in normal dev use Alembic manages migrations.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(200) NOT NULL,
    brief JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    total_spend_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_revenue_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns(tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id VARCHAR(64) NOT NULL,
    campaign_id UUID NOT NULL REFERENCES campaigns(id),
    request_id UUID NOT NULL,
    agent_name VARCHAR(64) NOT NULL,
    input_payload JSONB NOT NULL DEFAULT '{}',
    output_payload JSONB NOT NULL DEFAULT '{}',
    usage JSONB NOT NULL DEFAULT '{}',
    trace_id VARCHAR(128),
    approved_by_compliance BOOLEAN,
    error TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_campaign ON agent_runs(campaign_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant ON agent_runs(tenant_id);

CREATE TABLE IF NOT EXISTS kpi_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id VARCHAR(64) NOT NULL,
    campaign_id UUID NOT NULL REFERENCES campaigns(id),
    channel VARCHAR(32) NOT NULL,
    campaign_external_id VARCHAR(128),
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    spend_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    conversions INTEGER NOT NULL DEFAULT 0,
    revenue_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    ctr DOUBLE PRECISION NOT NULL DEFAULT 0,
    cvr DOUBLE PRECISION NOT NULL DEFAULT 0,
    cpc DOUBLE PRECISION NOT NULL DEFAULT 0,
    cpa DOUBLE PRECISION NOT NULL DEFAULT 0,
    cpm DOUBLE PRECISION NOT NULL DEFAULT 0,
    roas DOUBLE PRECISION NOT NULL DEFAULT 0,
    captured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kpi_campaign ON kpi_snapshots(campaign_id);
CREATE INDEX IF NOT EXISTS idx_kpi_channel ON kpi_snapshots(channel);
CREATE INDEX IF NOT EXISTS idx_kpi_tenant ON kpi_snapshots(tenant_id);

CREATE TABLE IF NOT EXISTS creative_artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id VARCHAR(64) NOT NULL,
    campaign_id UUID NOT NULL REFERENCES campaigns(id),
    agent_name VARCHAR(64) NOT NULL DEFAULT 'creative_strategist',
    channel VARCHAR(32),
    headline VARCHAR(200) NOT NULL,
    primary_text TEXT NOT NULL,
    cta VARCHAR(64) NOT NULL,
    visual_direction TEXT NOT NULL,
    ab_variants JSONB NOT NULL DEFAULT '{}',
    approved_by_compliance BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    campaign_id UUID,
    agent_name VARCHAR(64),
    request_id UUID,
    payload JSONB NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant ON audit_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred ON audit_events(occurred_at);
