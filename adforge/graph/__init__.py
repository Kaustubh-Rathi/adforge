"""LangGraph orchestrator — wires all 7 agents into a state graph.

Topology:
    START
      ↓
    research ──────────────┐
      ↓                    │
    creative_strategist ←─┘ (research feeds creative)
      ↓
    media_planner  ← creative feeds media plan
      ↓
    compliance     ← creative + media plan reviewed
      ↓ (if approved)         ↓ (if not approved)
    kpi_analytics              → creative_strategist (revise loop)
      ↓
    roi_calculator
      ↓
    optimization
      ↓
    END

State is held in a TypedDict that flows through every node. The Postgres
checkpointer persists intermediate state so a crashed graph can resume.
"""
from adforge.graph.state import CampaignState, build_graph, get_compiled_graph

__all__ = ["CampaignState", "build_graph", "get_compiled_graph"]
