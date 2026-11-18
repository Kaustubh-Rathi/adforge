"""Research Agent — LangGraph node + LLM message builder."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.parsing import extract_json, schema_instruction, validate_payload
from adforge.agents.research.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, ResearchFindings


class ResearchAgent(BaseAgent):
    name = "research"
    persona = PERSONA
    mcp_servers = ("research", "public_data")

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief
        brief_text = brief.model_dump_json(indent=2) if brief else "(no brief provided)"

        upstream = json.dumps(agent_input.upstream_payloads, default=str)

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief_text}\n\n"
                    f"Upstream agent outputs (may be empty):\n{upstream}\n\n"
                    "Produce a ResearchFindings JSON object. Use the MCP tools "
                    "available to you (web_search, serp_scrape, keyword_clusters, "
                    "competitor_creatives) before finalising your answer. "
                    "Respond with ONLY the JSON object — no prose."
                    f"{schema_instruction(ResearchFindings)}"
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        data = extract_json(content)
        return validate_payload(ResearchFindings, data, self.name)
