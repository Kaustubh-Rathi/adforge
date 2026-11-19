"""Campaigns API — CRUD + run-the-graph endpoint."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant, get_tenant_scope
from adforge.graph.state import clear_thread, get_compiled_graph, graph_config
from adforge.guardrails.contracts import CampaignBrief
from adforge.guardrails.data_isolation import TenantScope

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
            "VALUES (:id, :tid, :name, CAST(:brief AS JSONB), 'draft')"
        ),
        {
            "id": str(campaign_id),
            "tid": tenant_id,
            "name": body.name,
            "brief": body.brief.model_dump_json(),
        },
    )
    return CampaignResponse(
        id=campaign_id,
        tenant_id=tenant_id,
        name=body.name,
        brief=body.brief.model_dump(),
        status="draft",
        total_spend_usd=0.0,
        total_revenue_usd=0.0,
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
            id=r.id,
            tenant_id=r.tenant_id,
            name=r.name,
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
        id=r.id,
        tenant_id=r.tenant_id,
        name=r.name,
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
        "approval_token": None,
        "errors": [],
    }

    # Update campaign status to 'designing'
    await db.execute(
        sql_text("UPDATE campaigns SET status = 'designing' WHERE id = :id AND tenant_id = :tid"),
        {"id": str(campaign_id), "tid": scope.tenant_id},
    )
    await db.commit()

    # Run the graph from a clean checkpoint. Re-running a previously approved
    # campaign must never inherit its old approval token or review state.
    graph = await get_compiled_graph()
    config = graph_config(campaign_id)
    await clear_thread(campaign_id)
    final_state = await graph.ainvoke(initial, config=config)

    # The graph pauses at the human_review interrupt. Persist the campaign
    # status so the review queue can find it.
    snapshot = await graph.aget_state(config)
    pending = set(snapshot.next or ())
    review_status = final_state.get("review_status")

    if "human_review" in pending:
        new_status = "reviewing"
    elif review_status == "auto_blocked" or (
        review_status == "rejected" and final_state.get("revision_count", 0) >= 3
    ):
        new_status = "blocked"
    elif final_state.get("errors"):
        new_status = "failed"
    else:
        # Approval authorises publishing; the campaign is not live until the
        # explicit publish endpoint succeeds against a configured adapter.
        new_status = "approved"

    await db.execute(
        sql_text("UPDATE campaigns SET status = :status WHERE id = :id AND tenant_id = :tid"),
        {"id": str(campaign_id), "tid": scope.tenant_id, "status": new_status},
    )

    # ``__interrupt__`` carries non-JSON-serialisable LangGraph objects.
    # The approval token is a bearer credential: it must never leave the
    # server except through the approval-gated publish path.
    public_state = {
        k: v for k, v in final_state.items() if not k.startswith("__") and k != "approval_token"
    }

    return {
        "campaign_id": str(campaign_id),
        "request_id": initial["request_id"],
        "status": new_status,
        "paused_for_review": new_status == "reviewing",
        "final_state": public_state,
    }
