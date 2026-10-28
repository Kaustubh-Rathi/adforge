"""Campaigns API — CRUD + run-the-graph endpoint."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant, get_tenant_scope
from adforge.guardrails.contracts import CampaignBrief
from adforge.guardrails.data_isolation import TenantScope
from adforge.graph.state import get_compiled_graph

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class CreateCampaignRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    brief: CampaignBrief


class CampaignResponse(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    brief: dict[str, Any]
    status: str
    total_spend_usd: float
    total_revenue_usd: float


@router.post("", response_model=CampaignResponse, status_code=201)
async def create_campaign(
    body: CreateCampaignRequest,
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> CampaignResponse:
    """Create a new campaign. Returns the new campaign row."""
    campaign_id = uuid4()
    await db.execute(
        sql_text(
            "INSERT INTO campaigns (id, tenant_id, name, brief, status) "
            "VALUES (:id, :tid, :name, :brief::jsonb, 'draft')"
        ),
        {
            "id": str(campaign_id),
            "tid": tenant_id,
            "name": body.name,
            "brief": body.brief.model_dump_json(),
        },
    )
    return CampaignResponse(
        id=campaign_id, tenant_id=tenant_id, name=body.name,
        brief=body.brief.model_dump(), status="draft",
        total_spend_usd=0.0, total_revenue_usd=0.0,
    )


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignResponse]:
    result = await db.execute(
        sql_text(
            "SELECT id, tenant_id, name, brief, status, total_spend_usd, total_revenue_usd "
            "FROM campaigns WHERE tenant_id = :tid ORDER BY created_at DESC LIMIT 100"
        ),
        {"tid": tenant_id},
    )
    rows = result.fetchall()
    return [
        CampaignResponse(
            id=r.id, tenant_id=r.tenant_id, name=r.name,
            brief=r.brief if isinstance(r.brief, dict) else json.loads(r.brief),
            status=r.status,
            total_spend_usd=float(r.total_spend_usd),
            total_revenue_usd=float(r.total_revenue_usd),
        )
        for r in rows
    ]


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> CampaignResponse:
    result = await db.execute(
        sql_text(
            "SELECT id, tenant_id, name, brief, status, total_spend_usd, total_revenue_usd "
            "FROM campaigns WHERE id = :id AND tenant_id = :tid"
        ),
        {"id": str(campaign_id), "tid": tenant_id},
    )
    r = result.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse(
        id=r.id, tenant_id=r.tenant_id, name=r.name,
        brief=r.brief if isinstance(r.brief, dict) else json.loads(r.brief),
        status=r.status,
        total_spend_usd=float(r.total_spend_usd),
        total_revenue_usd=float(r.total_revenue_usd),
    )


@router.post("/{campaign_id}/run")
async def run_campaign(
    campaign_id: UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Kick off the LangGraph orchestrator for a campaign.

    Returns the final state after all 7 agents have run.
    """
    # Load the campaign brief from DB
    result = await db.execute(
        sql_text("SELECT brief FROM campaigns WHERE id = :id AND tenant_id = :tid"),
        {"id": str(campaign_id), "tid": scope.tenant_id},
    )
    r = result.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Campaign not found")
    brief = r.brief if isinstance(r.brief, dict) else json.loads(r.brief)

    # Build initial graph state
    initial: dict[str, Any] = {
        "request_id": str(uuid4()),
        "tenant_id": scope.tenant_id,
        "campaign_id": str(campaign_id),
        "brief": brief,
        "errors": [],
    }

    # Update campaign status to 'designing'
    await db.execute(
        sql_text("UPDATE campaigns SET status = 'designing' WHERE id = :id"),
        {"id": str(campaign_id)},
    )

    # Run the graph
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": f"{campaign_id}:{initial['request_id']}"}}
    final_state = await graph.ainvoke(initial, config=config)

    # Update campaign status to 'live' if no errors
    if not final_state.get("errors"):
        await db.execute(
            sql_text(
                "UPDATE campaigns SET status = 'live', started_at = :started WHERE id = :id"
            ),
            {"id": str(campaign_id), "started": datetime.now(timezone.utc)},
        )

    return {
        "campaign_id": str(campaign_id),
        "request_id": initial["request_id"],
        "final_state": final_state,
    }
