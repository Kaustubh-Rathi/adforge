"""Analytics MCP server — KPI snapshot persistence + queries.

Tools:
    persist_kpi_snapshot(snapshot_json)
    query_kpis(campaign_id, channel=None, days_back=7)
    aggregate_kpis(campaign_id, group_by="channel")

The KPI/Analytics agent calls persist_kpi_snapshot after fetching from the
ad platforms. The dashboard calls query_kpis / aggregate_kpis to render charts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text as sql_text

from adforge.mcp_servers.permissions import readonly
from adforge.storage.session import get_session_factory

mcp = FastMCP("adforge-analytics")


@mcp.tool()
@readonly
async def persist_kpi_snapshot(snapshot_json: str, headers: dict[str, str] | None = None) -> str:
    """Persist a KpiSnapshot row. Returns the new row id."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    tenant_id = headers["X-AdForge-Tenant"]
    snap = json.loads(snapshot_json)
    snap["tenant_id"] = tenant_id  # force tenant

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            sql_text(
                "INSERT INTO kpi_snapshots "
                "(tenant_id, campaign_id, channel, campaign_external_id, impressions, clicks, "
                " spend_usd, conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, captured_at) "
                "VALUES (:tid, :cid, :ch, :ext, :imp, :clk, :spd, :cnv, :rev, "
                "        :ctr, :cvr, :cpc, :cpa, :cpm, :roas, :cap) RETURNING id"
            ),
            {
                "tid": tenant_id,
                "cid": snap["campaign_id"],
                "ch": snap["channel"],
                "ext": snap.get("campaign_external_id"),
                "imp": snap["impressions"],
                "clk": snap["clicks"],
                "spd": snap["spend_usd"],
                "cnv": snap["conversions"],
                "rev": snap["revenue_usd"],
                "ctr": snap["ctr"],
                "cvr": snap["cvr"],
                "cpc": snap["cpc"],
                "cpa": snap["cpa"],
                "cpm": snap["cpm"],
                "roas": snap["roas"],
                "cap": snap.get("captured_at", datetime.now(timezone.utc).isoformat()),
            },
        )
        row_id = result.scalar_one()
        await session.commit()
    return json.dumps({"id": str(row_id), "status": "persisted"})


@mcp.tool()
async def query_kpis(
    campaign_id: str,
    channel: str | None = None,
    days_back: int = 7,
    headers: dict[str, str] | None = None,
) -> str:
    """Return KPI snapshots for the last N days, optionally filtered by channel."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    tenant_id = headers["X-AdForge-Tenant"]
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    factory = get_session_factory()
    async with factory() as session:
        if channel:
            q = sql_text(
                "SELECT * FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND channel = :ch "
                "AND captured_at >= :since ORDER BY captured_at DESC"
            )
            result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id, "ch": channel, "since": since})
        else:
            q = sql_text(
                "SELECT * FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND captured_at >= :since "
                "ORDER BY captured_at DESC"
            )
            result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id, "since": since})
        rows = [dict(r._mapping) for r in result]
    # Convert datetimes + UUIDs to strings for JSON
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, (datetime,)):
                r[k] = v.isoformat()
    return json.dumps({"snapshots": rows, "count": len(rows)})


@mcp.tool()
async def aggregate_kpis(
    campaign_id: str, group_by: str = "channel", headers: dict[str, str] | None = None
) -> str:
    """Aggregate KPIs by channel (default) or by day."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    tenant_id = headers["X-AdForge-Tenant"]
    if group_by not in ("channel", "day"):
        return json.dumps({"error": "group_by must be 'channel' or 'day'"})

    col = "channel" if group_by == "channel" else "DATE(captured_at)"
    factory = get_session_factory()
    async with factory() as session:
        q = sql_text(
            f"SELECT {col} AS bucket, "
            " SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
            " SUM(spend_usd) AS spend_usd, SUM(conversions) AS conversions, "
            " SUM(revenue_usd) AS revenue_usd "
            "FROM kpi_snapshots "
            "WHERE tenant_id = :tid AND campaign_id = :cid "
            f"GROUP BY {col} ORDER BY bucket"
        )
        result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id})
        rows = [dict(r._mapping) for r in result]
    return json.dumps({"group_by": group_by, "buckets": rows})
