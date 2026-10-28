"""ROI Calculator agent node."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.roi_calculator.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, RoiReport


class RoiCalculatorAgent(BaseAgent):
    name = "roi_calculator"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        kpis = json.dumps(agent_input.upstream_payloads.get("kpi_analytics", {}), indent=2, default=str)
        media_plan = json.dumps(agent_input.upstream_payloads.get("media_plan", {}), indent=2, default=str)

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief}\n\n"
                    f"Media plan:\n{media_plan}\n\n"
                    f"Latest KPI snapshots:\n{kpis}\n\n"
                    "Produce an RoiReport JSON object. Be specific in the "
                    "rationale — cite which channel drove / tanked the result."
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        body = content.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1]
            if body.endswith("```"):
                body = body.rsplit("```", 1)[0]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            data = {
                "total_spend_usd": 0,
                "total_revenue_usd": 0,
                "total_conversions": 0,
                "blended_roas": 0,
                "blended_cpa": 0,
                "net_profit_usd": 0,
                "recommendation": "pause",
                "rationale": f"unable to parse LLM output: {e}",
            }
        report = RoiReport(**data)
        return report.model_dump()
