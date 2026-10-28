"""Traces API — read agent runs + audit events for the dashboard's trace page."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("/{campaign_id}/runs")
async def list_agent_runs(
    campaign_id: UUID,
    agent_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all agent runs for a campaign. Newest first."""
    if agent_name:
        result = await db.execute(
            sql_text(
                "SELECT id, request_id, agent_name, input_payload, output_payload, "
                "       usage, error, status, started_at, finished_at "
                "FROM agent_runs "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND agent_name = :an "
                "ORDER BY started_at DESC LIMIT :lim"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "an": agent_name, "lim": limit},
        )
    else:
        result = await db.execute(
            sql_text(
                "SELECT id, request_id, agent_name, input_payload, output_payload, "
                "       usage, error, status, started_at, finished_at "
                "FROM agent_runs "
                "WHERE tenant_id = :tid AND campaign_id = :cid "
                "ORDER BY started_at DESC LIMIT :lim"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "lim": limit},
        )
    rows = []
    for r in result.fetchall():
        rows.append({
            "id": str(r.id),
            "request_id": str(r.request_id),
            "agent_name": r.agent_name,
            "input_payload": r.input_payload if isinstance(r.input_payload, dict) else json.loads(r.input_payload or "{}"),
            "output_payload": r.output_payload if isinstance(r.output_payload, dict) else json.loads(r.output_payload or "{}"),
            "usage": r.usage if isinstance(r.usage, dict) else json.loads(r.usage or "{}"),
            "error": r.error,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "duration_seconds": (
                (r.finished_at - r.started_at).total_seconds()
                if r.finished_at and r.started_at else None
            ),
        })
    return {"runs": rows, "count": len(rows)}


@router.get("/{campaign_id}/audit")
async def list_audit_events(
    campaign_id: UUID,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List audit events for a campaign. Newest first."""
    if event_type:
        result = await db.execute(
            sql_text(
                "SELECT event_id, event_type, agent_name, request_id, payload, occurred_at "
                "FROM audit_events "
                "WHERE tenant_id = :tid AND campaign_id = :cid AND event_type = :et "
                "ORDER BY occurred_at DESC LIMIT :lim"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "et": event_type, "lim": limit},
        )
    else:
        result = await db.execute(
            sql_text(
                "SELECT event_id, event_type, agent_name, request_id, payload, occurred_at "
                "FROM audit_events "
                "WHERE tenant_id = :tid AND campaign_id = :cid "
                "ORDER BY occurred_at DESC LIMIT :lim"
            ),
            {"tid": tenant_id, "cid": str(campaign_id), "lim": limit},
        )
    rows = []
    for r in result.fetchall():
        rows.append({
            "event_id": str(r.event_id),
            "event_type": r.event_type,
            "agent_name": r.agent_name,
            "request_id": str(r.request_id) if r.request_id else None,
            "payload": r.payload if isinstance(r.payload, dict) else json.loads(r.payload or "{}"),
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        })
    return {"events": rows, "count": len(rows)}
