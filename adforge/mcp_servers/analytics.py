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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text as sql_text

from adforge.mcp_servers.permissions import READ_ONLY, WRITE, readonly, write_operation
from adforge.storage import session as session_module

mcp = FastMCP("adforge-analytics")


def _coerce_date(value: object) -> date:
    """Accept a date, datetime, or ISO-ish string from JSON callers."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    # Slice off time components ("2026-01-05T..." / "20260105120000"). On
    # Python 3.11+ the compact form also tolerates a trailing suffix, but the
    # slice keeps date-only and timestamp inputs on one deterministic path.
    return date.fromisoformat(text[:10])


def _coerce_datetime(value: object) -> datetime:
    """Accept a datetime or ISO-ish string from JSON callers (asyncpg is strict)."""
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.combine(_coerce_date(text), datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert DB-native values (dates, UUIDs) into JSON-safe primitives."""
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (datetime, date)):
            clean[key] = value.isoformat()
        elif type(value).__name__ == "UUID":
            clean[key] = str(value)
        else:
            clean[key] = value
    return clean


@mcp.tool(annotations=WRITE)
@write_operation(requires_approval=False)
async def persist_kpi_snapshot(snapshot_json: str, headers: dict[str, str] | None = None) -> str:
    """Persist a KpiSnapshot row (idempotent daily upsert). Returns the row id."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    tenant_id = headers["X-AdForge-Tenant"]
    snap = json.loads(snapshot_json)
    snap["tenant_id"] = tenant_id  # force tenant
    if snap.get("is_mock"):
        return json.dumps({"error": "simulated KPI rows are not accepted"})
    platform_date = _coerce_date(
        snap.get("platform_date")
        or snap.get("captured_at", datetime.now(UTC).isoformat())
    )
    captured_at = _coerce_datetime(
        snap.get("captured_at", datetime.now(UTC).isoformat())
    )

    factory = session_module.get_session_factory()
    async with factory() as session:
        owned = await session.execute(
            sql_text(
                "SELECT 1 FROM campaigns WHERE id = :cid AND tenant_id = :tid"
            ),
            {"cid": str(snap["campaign_id"]), "tid": tenant_id},
        )
        if owned.fetchone() is None:
            return json.dumps({"error": "campaign not found for tenant"})
        result = await session.execute(
            sql_text(
                "INSERT INTO kpi_snapshots "
                "(tenant_id, campaign_id, channel, campaign_external_id, impressions, clicks, "
                " spend_usd, conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, "
                " downloads, source, is_mock, platform_date, captured_at) "
                "VALUES (:tid, :cid, :ch, :ext, :imp, :clk, :spd, :cnv, :rev, "
                "        :ctr, :cvr, :cpc, :cpa, :cpm, :roas, "
                "        :dl, :src, :mock, :pdate, :cap) "
                "ON CONFLICT (tenant_id, campaign_id, channel, platform_date) "
                "DO UPDATE SET impressions = EXCLUDED.impressions, "
                " clicks = EXCLUDED.clicks, spend_usd = EXCLUDED.spend_usd, "
                " conversions = EXCLUDED.conversions, revenue_usd = EXCLUDED.revenue_usd, "
                " ctr = EXCLUDED.ctr, cvr = EXCLUDED.cvr, cpc = EXCLUDED.cpc, "
                " cpa = EXCLUDED.cpa, cpm = EXCLUDED.cpm, roas = EXCLUDED.roas, "
                " downloads = EXCLUDED.downloads, source = EXCLUDED.source, "
                " is_mock = EXCLUDED.is_mock, captured_at = EXCLUDED.captured_at "
                "RETURNING id"
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
                "dl": snap.get("downloads", 0),
                "src": snap.get("source", "unknown"),
                "mock": snap.get("is_mock", False),
                "pdate": platform_date,
                "cap": captured_at,
            },
        )
        row_id = result.scalar_one()
        await session.commit()
    return json.dumps({"id": str(row_id), "status": "persisted"})


@mcp.tool(annotations=READ_ONLY)
@readonly
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
    since = (datetime.now(UTC) - timedelta(days=days_back)).date()
    factory = session_module.get_session_factory()
    async with factory() as session:
        if channel:
            q = sql_text(
                "SELECT * FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND channel = :ch "
                "AND platform_date >= :since ORDER BY platform_date DESC, captured_at DESC"
            )
            result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id, "ch": channel, "since": since})
        else:
            q = sql_text(
                "SELECT * FROM kpi_snapshots "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND platform_date >= :since "
                "ORDER BY platform_date DESC, captured_at DESC"
            )
            result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id, "since": since})
        rows = [_jsonable_row(dict(r._mapping)) for r in result]
    return json.dumps({"snapshots": rows, "count": len(rows)})


@mcp.tool(annotations=READ_ONLY)
@readonly
async def aggregate_kpis(
    campaign_id: str, group_by: str = "channel", headers: dict[str, str] | None = None
) -> str:
    """Aggregate KPIs by channel (default) or by day."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    tenant_id = headers["X-AdForge-Tenant"]
    if group_by not in ("channel", "day"):
        return json.dumps({"error": "group_by must be 'channel' or 'day'"})

    col = "channel" if group_by == "channel" else "platform_date"
    factory = session_module.get_session_factory()
    async with factory() as session:
        q = sql_text(
            f"SELECT {col} AS bucket, "
            " SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
            " SUM(spend_usd) AS spend_usd, SUM(conversions) AS conversions, "
            " SUM(revenue_usd) AS revenue_usd, "
            " BOOL_OR(is_mock) AS is_mock, BOOL_AND(is_mock) AS all_simulated "
            "FROM kpi_snapshots "
            "WHERE tenant_id = :tid AND campaign_id = :cid "
            f"GROUP BY {col} ORDER BY bucket"
        )
        result = await session.execute(q, {"tid": tenant_id, "cid": campaign_id})
        rows = [_jsonable_row(dict(r._mapping)) for r in result]
    # A bucket mixing real and simulated rows is never reported as fully real:
    # is_mock stays True, so downstream consumers disclose it.
    return json.dumps({"group_by": group_by, "buckets": rows})
