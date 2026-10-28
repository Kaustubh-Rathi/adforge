"""Reviews API — Human-in-the-Loop review queue + approve/reject endpoints.

Endpoints:
    GET    /reviews/pending                 — list campaigns waiting for review
    GET    /reviews/{campaign_id}/packet    — fetch the review packet (creative + plan + verdict)
    POST   /reviews/{campaign_id}/approve   — approve + resume graph + issue approval token
    POST   /reviews/{campaign_id}/reject    — reject + route graph back to creative_strategist

Auth:
    Every endpoint requires X-AdForge-Tenant header.
    Approve/reject additionally require X-AdForge-Reviewer header, which
    must be in the APP_REVIEWER_ALLOWLIST env var. (Empty allowlist = anyone
    can review — only safe in dev.)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant
from adforge.config import get_settings
from adforge.graph.state import get_compiled_graph
from langgraph.types import Command

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewDecision(BaseModel):
    approved: bool
    comments: str = Field(default="", max_length=2000)


def _assert_reviewer_allowed(reviewer_email: str | None) -> None:
    """Reject if reviewer not in allowlist (when allowlist is configured)."""
    s = get_settings()
    if not s.app_reviewer_allowlist:
        # Empty allowlist = dev mode, anyone can review
        return
    allowed = [e.strip().lower() for e in s.app_reviewer_allowlist.split(",") if e.strip()]
    if not reviewer_email or reviewer_email.lower() not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Reviewer {reviewer_email!r} not in allowlist. Allowed: {allowed}",
        )


@router.get("/pending")
async def list_pending_reviews(
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List campaigns that are currently paused at the human_review interrupt."""
    # Campaigns in 'reviewing' status (set when graph hits the interrupt)
    result = await db.execute(
        sql_text(
            "SELECT id, name, brief, status, started_at "
            "FROM campaigns WHERE tenant_id = :tid AND status = 'reviewing' "
            "ORDER BY started_at DESC"
        ),
        {"tid": tenant_id},
    )
    rows = []
    for r in result.fetchall():
        rows.append({
            "id": str(r.id),
            "name": r.name,
            "brief": r.brief if isinstance(r.brief, dict) else json.loads(r.brief or "{}"),
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
        })
    return {"pending": rows, "count": len(rows)}


@router.get("/{campaign_id}/packet")
async def get_review_packet(
    campaign_id: UUID,
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch the review packet for a paused campaign — what the reviewer sees."""
    # Find the most recent agent_runs for this campaign to surface
    # the creative + media plan + compliance verdict.
    result = await db.execute(
        sql_text(
            "SELECT agent_name, output_payload FROM agent_runs "
            "WHERE tenant_id = :tid AND campaign_id = :cid AND status = 'succeeded' "
            "AND agent_name IN ('creative_strategist', 'media_planner', 'compliance') "
            "ORDER BY started_at DESC LIMIT 3"
        ),
        {"tid": tenant_id, "cid": str(campaign_id)},
    )
    packet = {"creative": {}, "media_plan": {}, "compliance_verdict": {}}
    for r in result.fetchall():
        payload = r.output_payload if isinstance(r.output_payload, dict) else json.loads(r.output_payload or "{}")
        packet[r.agent_name] = payload
    return {"campaign_id": str(campaign_id), "packet": packet}


@router.post("/{campaign_id}/approve")
async def approve_campaign(
    campaign_id: UUID,
    body: ReviewDecision,
    tenant_id: str = Depends(get_tenant),
    reviewer_email: str | None = Header(default=None, alias="X-AdForge-Reviewer"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Approve a paused campaign. Resumes the graph + issues an approval token
    that the orchestrator's publish step will use to call write MCP tools."""
    _assert_reviewer_allowed(reviewer_email)

    # Update campaign status
    await db.execute(
        sql_text("UPDATE campaigns SET status = 'approved' WHERE id = :id AND tenant_id = :tid"),
        {"id": str(campaign_id), "tid": tenant_id},
    )

    # Resume the graph with the approve Command
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": f"{campaign_id}"}}
    resume_payload = {
        "approved": True,
        "reviewer_email": reviewer_email,
        "comments": body.comments,
    }
    try:
        final_state = await graph.ainvoke(Command(resume=resume_payload), config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph resume failed: {e}")

    # Emit audit event for the approval decision
    await db.execute(
        sql_text(
            "INSERT INTO audit_events (event_type, tenant_id, campaign_id, agent_name, payload, occurred_at) "
            "VALUES ('compliance.decision', :tid, :cid, 'human_review', :p::jsonb, :occ)"
        ),
        {
            "tid": tenant_id, "cid": str(campaign_id),
            "p": json.dumps({
                "decision": "approved",
                "reviewer": reviewer_email,
                "comments": body.comments,
            }),
            "occ": datetime.now(timezone.utc),
        },
    )

    return {
        "campaign_id": str(campaign_id),
        "decision": "approved",
        "reviewer": reviewer_email,
        "final_state_keys": list(final_state.keys()) if isinstance(final_state, dict) else [],
    }


@router.post("/{campaign_id}/reject")
async def reject_campaign(
    campaign_id: UUID,
    body: ReviewDecision,
    tenant_id: str = Depends(get_tenant),
    reviewer_email: str | None = Header(default=None, alias="X-AdForge-Reviewer"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Reject a paused campaign. Routes the graph back to creative_strategist
    with the reviewer's comments, so the next creative attempt addresses them."""
    _assert_reviewer_allowed(reviewer_email)

    await db.execute(
        sql_text("UPDATE campaigns SET status = 'revising' WHERE id = :id AND tenant_id = :tid"),
        {"id": str(campaign_id), "tid": tenant_id},
    )

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": f"{campaign_id}"}}
    resume_payload = {
        "approved": False,
        "reviewer_email": reviewer_email,
        "comments": body.comments,
    }
    try:
        final_state = await graph.ainvoke(Command(resume=resume_payload), config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph resume failed: {e}")

    await db.execute(
        sql_text(
            "INSERT INTO audit_events (event_type, tenant_id, campaign_id, agent_name, payload, occurred_at) "
            "VALUES ('compliance.decision', :tid, :cid, 'human_review', :p::jsonb, :occ)"
        ),
        {
            "tid": tenant_id, "cid": str(campaign_id),
            "p": json.dumps({
                "decision": "rejected",
                "reviewer": reviewer_email,
                "comments": body.comments,
            }),
            "occ": datetime.now(timezone.utc),
        },
    )

    return {
        "campaign_id": str(campaign_id),
        "decision": "rejected",
        "reviewer": reviewer_email,
        "routed_to": "creative_strategist (revision)",
        "final_state_keys": list(final_state.keys()) if isinstance(final_state, dict) else [],
    }
