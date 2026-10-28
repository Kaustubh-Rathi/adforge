"""KPIs API — query KPI snapshots for the dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant

router = APIRouter(prefix="/kpis", tags=["kpis"])


@router.get("/{campaign_id}/snapshots")
async def list_kpis(
    campaign_id: UUID,
    days_back: int = Query(default=7, ge=1, le=90),
    channel: str | None = None,
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List KPI snapshots for a campaign. Optionally filter by channel."""
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    if channel:
        result = await db.execute(
            sql_text(
                "SELECT id, channel, campaign_external_id, impressions, clicks, spend_usd, "
                "       conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, captured_at "
                "FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND channel = :ch "
                "AND captured_at >= :since ORDER BY captured_at ASC"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "ch": channel, "since": since},
        )
    else:
        result = await db.execute(
            sql_text(
                "SELECT id, channel, campaign_external_id, impressions, clicks, spend_usd, "
                "       conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, captured_at "
                "FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND captured_at >= :since "
                "ORDER BY captured_at ASC"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "since": since},
        )
    rows = []
    for r in result.fetchall():
        rows.append({
            "id": str(r.id),
            "channel": r.channel,
            "campaign_external_id": r.campaign_external_id,
            "impressions": r.impressions,
            "clicks": r.clicks,
            "spend_usd": float(r.spend_usd),
            "conversions": r.conversions,
            "revenue_usd": float(r.revenue_usd),
            "ctr": float(r.ctr),
            "cvr": float(r.cvr),
            "cpc": float(r.cpc),
            "cpa": float(r.cpa),
            "cpm": float(r.cpm),
            "roas": float(r.roas),
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        })
    return {"snapshots": rows, "count": len(rows)}


@router.get("/{campaign_id}/aggregate")
async def aggregate_kpis(
    campaign_id: UUID,
    group_by: str = Query(default="channel", pattern="^(channel|day)$"),
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate KPIs by channel or by day."""
    col = "channel" if group_by == "channel" else "DATE(captured_at)"
    result = await db.execute(
        sql_text(
            f"SELECT {col} AS bucket, "
            "       SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
            "       SUM(spend_usd) AS spend_usd, SUM(conversions) AS conversions, "
            "       SUM(revenue_usd) AS revenue_usd "
            "FROM kpi_snapshots "
            "WHERE tenant_id = :tid AND campaign_id = :cid "
            f"GROUP BY {col} ORDER BY bucket"
        ),
        {"tid": tenant_id, "cid": str(campaign_id)},
    )
    buckets = []
    for r in result.fetchall():
        spend = float(r.spend_usd or 0)
        revenue = float(r.revenue_usd or 0)
        conversions = int(r.conversions or 0)
        impressions = int(r.impressions or 0)
        clicks = int(r.clicks or 0)
        buckets.append({
            "bucket": r.bucket if not isinstance(r.bucket, datetime) else r.bucket.isoformat(),
            "impressions": impressions,
            "clicks": clicks,
            "spend_usd": spend,
            "conversions": conversions,
            "revenue_usd": revenue,
            "roas": round(revenue / spend, 2) if spend else 0.0,
            "cpa": round(spend / conversions, 2) if conversions else 0.0,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        })
    return {"group_by": group_by, "buckets": buckets}
