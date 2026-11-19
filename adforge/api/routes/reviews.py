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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.api.deps import get_db, get_tenant
from adforge.config import get_settings
from adforge.graph.state import get_compiled_graph, graph_config
from adforge.publishing import PublishError, publish_approved_campaign

router = APIRouter(prefix="/reviews", tags=["reviews"])

#: agent_runs.agent_name → review-packet key
_PACKET_KEYS = {
    "creative_strategist": "creative",
    "media_planner": "media_plan",
    "compliance": "compliance_verdict",
}


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


async def _assert_paused_at_gate(graph: Any, config: RunnableConfig) -> None:
    """Refuse to resume unless a checkpoint is parked at ``human_review``.

    A resume against a missing checkpoint does not fail: LangGraph simply
    starts the graph over, re-running (and re-billing) research, creative and
    media planning before pausing again. That would quietly ignore the human
    decision, so treat it as a conflict instead.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:  # noqa: BLE001 — surface as a conflict, not a 500
        raise HTTPException(
            status_code=409,
            detail=f"could not read this campaign's graph state: {e}",
        ) from e
    if "human_review" not in set(snapshot.next or ()):
        raise HTTPException(
            status_code=409,
            detail=(
                "no paused human-review checkpoint for this campaign; the graph "
                "state is missing (a restart while the checkpointer was not "
                "durable does this). Re-run the campaign before reviewing it."
            ),
        )


async def _resume_graph(
    campaign_id: UUID,
    reviewer_email: str | None,
    comments: str,
    approved: bool,
) -> tuple[dict[str, Any], str]:
    """Resume the paused graph and derive the campaign's next status."""
    graph = await get_compiled_graph()
    config = graph_config(campaign_id)
    await _assert_paused_at_gate(graph, config)
    resume_payload = {
        "approved": approved,
        "reviewer_email": reviewer_email,
        "comments": comments,
    }
    try:
        final_state = await graph.ainvoke(Command(resume=resume_payload), config=config)
        snapshot = await graph.aget_state(config)
    except Exception as e:  # noqa: BLE001 — surface as a 500 with context
        raise HTTPException(status_code=500, detail=f"Graph resume failed: {e}") from e

    pending = set(snapshot.next or ())
    if "human_review" in pending:
        # Rejected again → graph paused for another review round.
        status = "reviewing"
    elif final_state.get("review_status") == "auto_blocked" or (
        final_state.get("review_status") == "rejected" and final_state.get("revision_count", 0) >= 3
    ):
        status = "blocked"
    elif approved and final_state.get("review_status") == "approved":
        # Approval authorises publishing; it does not launch anything.
        # Post-approval analytics can fail without ad-account credentials, but
        # that must not revoke the human approval or block the publish path.
        status = "approved"
    elif final_state.get("errors"):
        status = "failed"
    else:
        status = "reviewing"
    return final_state, status


async def _update_campaign_status(
    db: AsyncSession, campaign_id: UUID, tenant_id: str, status: str
) -> None:
    if status == "live":
        await db.execute(
            sql_text(
                "UPDATE campaigns SET status = :status, started_at = :started "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {
                "id": str(campaign_id),
                "tid": tenant_id,
                "status": status,
                "started": datetime.now(UTC),
            },
        )
    else:
        await db.execute(
            sql_text("UPDATE campaigns SET status = :status WHERE id = :id AND tenant_id = :tid"),
            {"id": str(campaign_id), "tid": tenant_id, "status": status},
        )


async def _assert_campaign_owned(db: AsyncSession, campaign_id: UUID, tenant_id: str) -> None:
    """Refuse to resume a graph for a campaign the caller doesn't own."""
    result = await db.execute(
        sql_text("SELECT 1 FROM campaigns WHERE id = :cid AND tenant_id = :tid"),
        {"cid": str(campaign_id), "tid": tenant_id},
    )
    if result.fetchone() is None:
        raise HTTPException(status_code=404, detail="Campaign not found")


async def _assert_campaign_status(
    db: AsyncSession, campaign_id: UUID, tenant_id: str, expected: str
) -> None:
    """Fail closed unless the campaign is in the status this action requires."""
    result = await db.execute(
        sql_text("SELECT status FROM campaigns WHERE id = :cid AND tenant_id = :tid"),
        {"cid": str(campaign_id), "tid": tenant_id},
    )
    row = result.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if row.status != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"campaign must be {expected!r} for this action (current status: {row.status!r})"
            ),
        )


async def _record_review_event(
    db: AsyncSession,
    campaign_id: UUID,
    tenant_id: str,
    reviewer_email: str | None,
    decision: str,
    comments: str,
) -> None:
    event_id = uuid4()
    await db.execute(
        sql_text(
            "INSERT INTO audit_events "
            "(event_id, event_type, tenant_id, campaign_id, agent_name, payload, occurred_at) "
            "VALUES (:eid, 'review.decision', :tid, :cid, 'human_review', CAST(:p AS JSONB), :occ)"
        ),
        {
            "eid": str(event_id),
            "tid": tenant_id,
            "cid": str(campaign_id),
            "p": json.dumps(
                {
                    "decision": decision,
                    "reviewer": reviewer_email,
                    "comments": comments,
                }
            ),
            "occ": datetime.now(UTC),
        },
    )


@router.get("/pending")
async def list_pending_reviews(
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List campaigns that are currently paused at the human_review interrupt."""
    result = await db.execute(
        sql_text(
            "SELECT id, name, brief, status, started_at "
            "FROM campaigns WHERE tenant_id = :tid AND status = 'reviewing' "
            "ORDER BY started_at DESC NULLS LAST"
        ),
        {"tid": tenant_id},
    )
    rows = []
    for r in result.fetchall():
        rows.append(
            {
                "id": str(r.id),
                "name": r.name,
                "brief": r.brief if isinstance(r.brief, dict) else json.loads(r.brief or "{}"),
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
        )
    return {"pending": rows, "count": len(rows)}


@router.get("/{campaign_id}/packet")
async def get_review_packet(
    campaign_id: UUID,
    tenant_id: str = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch the review packet for a paused campaign — what the reviewer sees."""
    campaign = await db.execute(
        sql_text(
            "SELECT id, name, status, brief FROM campaigns WHERE id = :cid AND tenant_id = :tid"
        ),
        {"cid": str(campaign_id), "tid": tenant_id},
    )
    row = campaign.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Latest successful run per agent, so revisions don't mix old + new copy.
    result = await db.execute(
        sql_text(
            "SELECT DISTINCT ON (agent_name) agent_name, output_payload "
            "FROM agent_runs "
            "WHERE tenant_id = :tid AND campaign_id = :cid AND status = 'succeeded' "
            "AND agent_name IN ('creative_strategist', 'media_planner', 'compliance') "
            "ORDER BY agent_name, started_at DESC"
        ),
        {"tid": tenant_id, "cid": str(campaign_id)},
    )

    packet: dict[str, Any] = {"creative": {}, "media_plan": {}, "compliance_verdict": {}}
    for r in result.fetchall():
        payload = (
            r.output_payload
            if isinstance(r.output_payload, dict)
            else json.loads(r.output_payload or "{}")
        )
        key = _PACKET_KEYS.get(r.agent_name)
        if key:
            packet[key] = payload

    return {
        "campaign_id": str(campaign_id),
        "campaign": {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "brief": row.brief if isinstance(row.brief, dict) else json.loads(row.brief or "{}"),
        },
        "packet": packet,
    }


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
    if body.approved is not True:
        raise HTTPException(
            status_code=400,
            detail="approve requires body.approved=true; use /reject to reject",
        )
    await _assert_campaign_status(db, campaign_id, tenant_id, "reviewing")

    final_state, status = await _resume_graph(
        campaign_id, reviewer_email, body.comments, approved=True
    )
    await _update_campaign_status(db, campaign_id, tenant_id, status)
    await _record_review_event(
        db, campaign_id, tenant_id, reviewer_email, "approved", body.comments
    )

    return {
        "campaign_id": str(campaign_id),
        "decision": "approved",
        "reviewer": reviewer_email,
        "status": status,
        "approval_token_issued": bool(final_state.get("approval_token")),
        "next_step": (
            "POST /reviews/{campaign_id}/publish to launch via the configured publishing adapter"
        ),
        "final_state_keys": [k for k in final_state if not k.startswith("__")],
    }


async def _record_publish_event(
    db: AsyncSession,
    campaign_id: UUID,
    tenant_id: str,
    reviewer_email: str,
    results: list[dict[str, Any]],
) -> None:
    await db.execute(
        sql_text(
            "INSERT INTO audit_events "
            "(event_id, event_type, tenant_id, campaign_id, agent_name, payload, occurred_at) "
            "VALUES (:eid, 'publish.executed', :tid, :cid, 'orchestrator', CAST(:p AS JSONB), :occ)"
        ),
        {
            "eid": str(uuid4()),
            "tid": tenant_id,
            "cid": str(campaign_id),
            "p": json.dumps({"reviewer": reviewer_email, "published": results}, default=str),
            "occ": datetime.now(UTC),
        },
    )


@router.post("/{campaign_id}/publish")
async def publish_campaign(
    campaign_id: UUID,
    tenant_id: str = Depends(get_tenant),
    reviewer_email: str | None = Header(default=None, alias="X-AdForge-Reviewer"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Launch an approved campaign through the configured publishing adapter.

    This is deliberately separate from approval: a human saying "yes" does not
    spend money. The single-use token minted by approval is consumed here, and
    only a successful adapter response moves the campaign to ``live``.
    """
    _assert_reviewer_allowed(reviewer_email)
    await _assert_campaign_owned(db, campaign_id, tenant_id)

    row = (
        await db.execute(
            sql_text("SELECT name, status FROM campaigns WHERE id = :cid AND tenant_id = :tid"),
            {"cid": str(campaign_id), "tid": tenant_id},
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if row.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=(f"campaign must be approved before publishing (current status: {row.status})"),
        )

    graph = await get_compiled_graph()
    config = graph_config(campaign_id)
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=409,
            detail=f"could not load the approved graph state: {exc}",
        ) from exc
    state = snapshot.values or {}
    approval_token = state.get("approval_token")
    if not approval_token:
        raise HTTPException(
            status_code=409,
            detail="no approval token found for this campaign; approve it first",
        )
    if not reviewer_email:
        raise HTTPException(status_code=403, detail="X-AdForge-Reviewer header is required")

    creative = state.get("creative", {}) or {}
    media_plan = state.get("media_plan", {}) or {}
    brief = state.get("brief") or {}
    campaign_name = str(row.name or brief.get("product_name") or "")

    try:
        results = await publish_approved_campaign(
            campaign_id=str(campaign_id),
            tenant_id=tenant_id,
            reviewer_email=reviewer_email,
            approval_token=approval_token,
            campaign_name=campaign_name,
            creative=creative,
            media_plan=media_plan,
            brief=brief,
        )
    except PublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await db.execute(
        sql_text(
            "UPDATE campaigns SET status = 'live', started_at = :started "
            "WHERE id = :cid AND tenant_id = :tid"
        ),
        {
            "cid": str(campaign_id),
            "tid": tenant_id,
            "started": datetime.now(UTC),
        },
    )
    await _record_publish_event(db, campaign_id, tenant_id, reviewer_email, results)

    return {
        "campaign_id": str(campaign_id),
        "status": "live",
        "reviewer": reviewer_email,
        "published": results,
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
    if body.approved is not False:
        raise HTTPException(
            status_code=400,
            detail="reject requires body.approved=false; use /approve to approve",
        )
    await _assert_campaign_status(db, campaign_id, tenant_id, "reviewing")

    final_state, status = await _resume_graph(
        campaign_id, reviewer_email, body.comments, approved=False
    )
    await _update_campaign_status(db, campaign_id, tenant_id, status)
    await _record_review_event(
        db, campaign_id, tenant_id, reviewer_email, "rejected", body.comments
    )

    return {
        "campaign_id": str(campaign_id),
        "decision": "rejected",
        "reviewer": reviewer_email,
        "status": status,
        "routed_to": "creative_strategist (revision)",
        "final_state_keys": [k for k in final_state if not k.startswith("__")],
    }
