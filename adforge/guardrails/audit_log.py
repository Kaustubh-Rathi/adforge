"""Append-only audit log of every agent decision.

Every LLM call, every MCP tool call, every guardrail firing is recorded as an
``AuditEvent`` row in Postgres. The table is append-only — there's no UPDATE
or DELETE path. This is the forensic record we hand to compliance reviewers
when an advertiser asks "why did your agent pause my campaign?".

In production, this is also shipped to an external SIEM (Splunk/Datadog) via
the ``AuditLog.ship`` hook.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger("adforge.audit")


class AuditEventType(StrEnum):
    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    AGENT_ERROR = "agent.error"
    LLM_CALL = "llm.call"
    LLM_RESPONSE = "llm.response"
    MCP_TOOL_CALL = "mcp.tool_call"
    GUARDRAIL_FIRE = "guardrail.fire"
    COMPLIANCE_DECISION = "compliance.decision"
    DATA_ACCESS = "data.access"
    CAMPAIGN_STATUS = "campaign.status"
    REVIEW_DECISION = "review.decision"
    KPI_FETCH = "kpi.fetch"
    KPI_PERSIST = "kpi.persist"
    PUBLISH_EXECUTED = "publish.executed"
    BUDGET_REALLOC = "budget.realloc"
    RATE_LIMIT = "rate_limit.rejected"


class AuditEvent(BaseModel):
    """One audit event. Stored as JSONB in Postgres for queryability."""

    event_id: UUID = Field(default_factory=uuid4)
    event_type: AuditEventType
    tenant_id: str
    campaign_id: UUID | None = None
    agent_name: str | None = None
    request_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditLog:
    """Async audit log writer. Batches writes to amortise DB round-trips."""

    def __init__(self) -> None:
        self._buffer: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Record an event. Buffer in-memory; flushed by ``flush()``.

        In production we'd flush every N events or every T seconds via a
        background task. For v1 we flush on every record — simpler to reason
        about.
        """
        self._buffer.append(event)
        await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return

        from adforge.storage.session import get_session_factory

        events = self._buffer
        self._buffer = []
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                for e in events:
                    # Use raw SQL for the append-only audit table — no ORM model
                    # means no accidental UPDATE path.
                    from sqlalchemy import text

                    await session.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(event_id, event_type, tenant_id, campaign_id, agent_name, "
                            " request_id, payload, occurred_at) "
                            "VALUES (:eid, :et, :tid, :cid, :an, :rid, CAST(:p AS JSONB), :occ)"
                        ),
                        {
                            "eid": str(e.event_id),
                            "et": e.event_type.value,
                            "tid": e.tenant_id,
                            "cid": str(e.campaign_id) if e.campaign_id else None,
                            "an": e.agent_name,
                            "rid": str(e.request_id) if e.request_id else None,
                            "p": json.dumps(e.payload, default=str),
                            "occ": e.occurred_at,
                        },
                    )
                await session.commit()
        except Exception as exc:
            # Audit persistence must never crash an agent run. Re-buffer and
            # retry on the next flush; bound the buffer so a long outage does
            # not grow memory without limit. Log one concise line rather than
            # a full traceback for every event while the database is down.
            logger.warning(
                "audit flush failed (%s: %s); %d event(s) re-buffered",
                type(exc).__name__,
                exc,
                len(events),
            )
            self._buffer = (events + self._buffer)[-10_000:]


_audit_log: AuditLog | None = None


def get_audit_log() -> AuditLog:
    """Singleton accessor. Tests can replace via monkeypatch."""
    global _audit_log
    if _audit_log is None:
        _audit_log = AuditLog()
    return _audit_log
