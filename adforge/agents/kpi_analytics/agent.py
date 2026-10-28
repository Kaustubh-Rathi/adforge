"""KPI / Analytics agent node.

For v1 we don't drive this via the LLM — it's a deterministic data-fetcher.
We still extend BaseAgent so it gets guardrails + audit logging + persistence,
but build_messages returns a placeholder and parse_response just wraps the
already-fetched KPI list into the output payload.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from adforge.agents.base import BaseAgent
from adforge.agents.kpi_analytics.persona import PERSONA
from adforge.agents.kpi_analytics.fetcher import fetch_all_channels
from adforge.guardrails.contracts import AgentInput, KpiSnapshot


class KpiAnalyticsAgent(BaseAgent):
    name = "kpi_analytics"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        # The KPI agent doesn't actually call the LLM in v1 — it directly
        # fetches from ad platform MCPs. We keep build_messages for parity.
        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": "Fetch latest KPI snapshots for all channels in the MediaPlan.",
            },
        ]

    async def _execute(self, scope, agent_input):
        # Override BaseAgent._execute to skip the LLM call entirely.
        from adforge.guardrails.contracts import AgentOutput
        from adforge.guardrails.audit_log import AuditEvent, AuditEventType
        from adforge.mcp_servers.client import get_mcp_tools

        media_plan = agent_input.upstream_payloads.get("media_plan", {})
        channels = [a["channel"] for a in media_plan.get("channel_allocations", [])]
        if not channels and agent_input.brief:
            channels = agent_input.brief.channels_hint

        snapshots: list[dict[str, Any]] = []
        for channel in channels:
            try:
                snap = await fetch_all_channels(scope, channel, agent_input.campaign_id)
                if snap:
                    snapshots.append(snap.model_dump())
            except Exception as e:
                await self._audit.record(AuditEvent(
                    event_type=AuditEventType.MCP_TOOL_CALL,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"channel": channel, "error": str(e)},
                ))

        return AgentOutput(
            request_id=agent_input.request_id,
            tenant_id=scope.tenant_id,
            campaign_id=scope.campaign_id,
            agent_name=self.name,
            payload={"snapshots": snapshots, "channel_count": len(channels)},
            usage={"provider": "direct-mcp", "llm_calls": 0},
        )

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        # Never invoked — _execute is overridden.
        return {"snapshots": []}
