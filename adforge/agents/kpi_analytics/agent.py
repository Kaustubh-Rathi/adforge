"""KPI / Analytics agent node.

This node is deterministic: it fetches daily metrics from each channel in the
approved media plan through that channel's real reporting API, persists the
snapshots idempotently, and returns them as its payload. Missing credentials
or campaign ids surface as explicit errors.

Dev/test only: when a channel's ``*_USE_MOCK`` setting is enabled, the fetcher
short-circuits to deterministic simulated snapshots — every row is labelled
``is_mock=true`` and the payload's ``is_mock`` flag reflects that. Staging and
prod never take the mock path. It extends BaseAgent so it inherits tenant
isolation, audit logging, and run persistence.
"""
from __future__ import annotations

from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.kpi_analytics.fetcher import (
    fetch_channel_snapshots,
    persist_snapshots,
)
from adforge.agents.kpi_analytics.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, AgentOutput
from adforge.guardrails.exceptions import McpToolError


class KpiAnalyticsAgent(BaseAgent):
    name = "kpi_analytics"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        # The KPI agent does not call the LLM — it fetches data directly.
        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": "Fetch latest KPI snapshots for all channels in the MediaPlan.",
            },
        ]

    async def _execute(self, scope, agent_input: AgentInput) -> AgentOutput:
        from adforge.guardrails.audit_log import AuditEvent, AuditEventType

        media_plan = agent_input.upstream_payloads.get("media_plan", {})
        allocations = [
            a for a in media_plan.get("channel_allocations", []) if a.get("channel")
        ]
        if not allocations and agent_input.brief:
            allocations = [
                {"channel": channel} for channel in agent_input.brief.channels_hint
            ]

        snapshots: list[dict[str, Any]] = []
        failures: list[str] = []

        for allocation in allocations:
            channel = allocation["channel"]
            external_id = (
                allocation.get("campaign_external_id")
                or allocation.get("external_campaign_id")
            )
            try:
                channel_snapshots = await fetch_channel_snapshots(
                    scope,
                    channel,
                    agent_input.campaign_id,
                    external_id=external_id,
                    budget_usd=allocation.get("budget_usd"),
                )
                snapshots.extend(s.model_dump(mode="json") for s in channel_snapshots)
                await self._safe_audit(AuditEvent(
                    event_type=AuditEventType.KPI_FETCH,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={
                        "channel": channel,
                        "rows": len(channel_snapshots),
                        "source": channel_snapshots[0].source if channel_snapshots else "none",
                    },
                ))
            except Exception as e:
                failures.append(f"{channel}: {type(e).__name__}: {e}")
                await self._safe_audit(AuditEvent(
                    event_type=AuditEventType.MCP_TOOL_CALL,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"channel": channel, "error": str(e)},
                ))

        # Every channel failed → surface as an agent error so the graph records it.
        if allocations and not snapshots and failures:
            raise McpToolError(
                server="ad-platform",
                tool=f"kpi_fetch:{','.join(a['channel'] for a in allocations)}",
                detail="; ".join(failures)[:1000],
            )

        persisted = 0
        if snapshots:
            try:
                from adforge.guardrails.contracts import KpiSnapshot

                models = [KpiSnapshot(**row) for row in snapshots]
                persisted = await persist_snapshots(
                    scope, agent_input.campaign_id, models
                )
                await self._safe_audit(AuditEvent(
                    event_type=AuditEventType.KPI_PERSIST,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"rows": persisted},
                ))
            except Exception as e:
                failures.append(f"persist: {type(e).__name__}: {e}")
                await self._safe_audit(AuditEvent(
                    event_type=AuditEventType.KPI_PERSIST,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"error": str(e)},
                ))

        return AgentOutput(
            request_id=agent_input.request_id,
            tenant_id=scope.tenant_id,
            campaign_id=scope.campaign_id,
            agent_name=self.name,
            payload={
                "snapshots": snapshots,
                "channel_count": len(allocations),
                "persisted": persisted,
                "errors": failures,
                "is_mock": any(bool(s["is_mock"]) for s in snapshots),
            },
            usage={"provider": "ad-platform-http", "llm_calls": 0},
        )

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        # Never invoked — _execute is overridden.
        return {"snapshots": []}
