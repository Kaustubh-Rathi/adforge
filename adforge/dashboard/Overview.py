"""AdForge Streamlit dashboard — overview page.

Layout: ink masthead, live operations board (four figures), "how it works"
definition rows, then the three architecture diagrams with the agent pipeline
as the visual anchor. All visuals come from ``adforge.dashboard.theme`` so the
sub-pages stay consistent. All data comes from ``adforge.dashboard.api_client``
— the dashboard never calls the API directly.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from adforge.dashboard import api_client as api
from adforge.dashboard.theme import (
    af_badge,
    af_caption,
    af_legend,
    af_metric,
    af_section,
    inject_css,
    render_mermaid,
)

st.set_page_config(
    page_title="AdForge Overview",
    page_icon=":money_with_wings:",
    layout="wide",
    initial_sidebar_state="auto",
)

inject_css()

# The sidebar tenant switcher scopes every page's API calls (pages read the
# selection through api.current_tenant()).
api.tenant_switcher()

# --------------------------------------------------------------------------- #
# Masthead
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div class="af-hero">
      <div class="af-hero-kicker">multi-agent ad campaign platform</div>
      <div class="af-hero-title">AdForge</div>
      <div class="af-hero-tag">
        From a product brief to a launch decision: seven specialist agents research the
        market, design the creative, plan the media, check compliance, measure the KPIs
        and hand back a bottom-line ROI verdict &mdash; with a human in the loop.
      </div>
      <div class="af-hero-facts">
        <span><i>pipeline</i> one graph, seven agents</span>
        <span><i>review</i> you approve before launch</span>
        <span><i>integrations</i> 8 MCP servers</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="margin: -0.4rem 0 1.2rem; font-size: 0.92rem; color: #3B4552;">
      Work the console from the sidebar: create and run campaigns, read agent traces,
      clear the approval queue and check the ROI verdict.
    </div>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Live stats
# --------------------------------------------------------------------------- #
_REVIEW_STATUSES = {"reviewing", "pending", "awaiting_review", "in_review"}
_APPROVED_STATUSES = {"approved", "live", "launched", "running"}


def _load_campaigns() -> tuple[list[dict[str, Any]], str | None]:
    """Fetch campaigns through api_client; never raise to the UI."""
    try:
        return api.list_campaigns(), None
    except Exception as exc:  # API down / auth missing / network
        return [], str(exc)


def _is_mock_campaign(campaign: dict[str, Any]) -> bool:
    """True when a campaign payload is explicitly flagged as mock data."""
    brief = campaign.get("brief")
    brief = brief if isinstance(brief, dict) else {}
    markers = (
        campaign.get("is_mock"),
        campaign.get("kpi_is_mock"),
        brief.get("is_mock"),
        brief.get("use_mock"),
    )
    return any(bool(marker) for marker in markers)


def _uses_mock_data(campaigns: list[dict[str, Any]]) -> bool:
    """Decide whether to surface the mock-data note.

    Every campaign flagged ``is_mock`` -> yes. No flags at all but zero spend
    everywhere -> treat as mock too, which is exactly what the dev KPI fetcher
    returns before platform credentials are configured.
    """
    if not campaigns:
        return False
    flagged = [c for c in campaigns if _is_mock_campaign(c)]
    if flagged:
        return len(flagged) == len(campaigns)
    return all(float(c.get("total_spend_usd") or 0.0) == 0.0 for c in campaigns)


def _count_by_status(campaigns: list[dict[str, Any]], statuses: set[str]) -> int:
    return sum(1 for c in campaigns if str(c.get("status", "")).lower() in statuses)


campaigns, api_error = _load_campaigns()

af_section("Operations board", "Live counts from the AdForge API for this tenant")

stat1, stat2, stat3, stat4 = st.columns(4)
with stat1:
    af_metric(
        "Campaigns",
        str(len(campaigns)) if api_error is None else "—",
        help_text="total in this tenant",
    )
with stat2:
    af_metric(
        "Awaiting review",
        str(_count_by_status(campaigns, _REVIEW_STATUSES)) if api_error is None else "—",
        help_text="paused at the human_review gate",
    )
with stat3:
    af_metric(
        "Approved",
        str(_count_by_status(campaigns, _APPROVED_STATUSES)) if api_error is None else "—",
        help_text="cleared for launch",
    )
with stat4:
    spend = sum(float(c.get("total_spend_usd") or 0.0) for c in campaigns)
    af_metric(
        "Platform spend",
        f"${spend:,.0f}" if api_error is None else "—",
        help_text="summed across campaigns",
    )

if api_error:
    st.markdown(
        '<div style="margin-top:0.45rem;font-size:0.8rem;color:#6B7683">'
        f"{af_badge('failed', 'API offline')} The AdForge API is unreachable, so the "
        "figures above show a dash. Start the API on :8000 and reload this page.</div>",
        unsafe_allow_html=True,
    )
elif _uses_mock_data(campaigns):
    st.markdown(
        f'<div style="margin-top:0.45rem">{af_badge("mock", "Mock KPI data")} '
        '<span style="font-size:0.8rem;color:#6B7683">Ad-platform KPIs are deterministic '
        "mocks until real credentials are configured.</span></div>",
        unsafe_allow_html=True,
    )

af_caption("Stats are read live from api_client.list_campaigns() on every page render.")

# --------------------------------------------------------------------------- #
# How it works
# --------------------------------------------------------------------------- #
af_section("How it works", "Three things to know before you run a campaign")

how1, how2, how3 = st.columns(3)
with how1:
    st.markdown(
        '<div class="af-def">'
        '<div class="af-def-title">Brief in, agents out</div>'
        '<div class="af-def-body">Describe a product and a budget. Seven specialist '
        "agents research the market, write the creative, plan the media and score "
        "compliance.</div></div>",
        unsafe_allow_html=True,
    )
with how2:
    st.markdown(
        '<div class="af-def">'
        '<div class="af-def-title">Human in the loop</div>'
        '<div class="af-def-body">The graph pauses at human_review and waits for an '
        "explicit approve or reject. Approval mints a single-use HMAC token; rejection "
        "loops the creative with your comments.</div></div>",
        unsafe_allow_html=True,
    )
with how3:
    st.markdown(
        '<div class="af-def">'
        '<div class="af-def-title">Money verdict</div>'
        '<div class="af-def-body">KPI analytics feed the ROI calculator, which answers '
        "the only question that matters: was this campaign worth the money it "
        "spent?</div></div>",
        unsafe_allow_html=True,
    )

# --------------------------------------------------------------------------- #
# Architecture diagrams
# --------------------------------------------------------------------------- #
_PIPELINE = """flowchart TB
    subgraph PLAN["Plan and build"]
        direction LR
        RS["fa:fa-magnifying-glass research<br/>market + audience scan"]
        CR["fa:fa-palette creative_strategist<br/>concept + copy"]
        MP["fa:fa-bullhorn media_planner<br/>channels + budget split"]
        CO["fa:fa-shield-halved compliance<br/>policy verdict"]
        RS --> CR --> MP --> CO
    end

    HR{"fa:fa-user-check human_review<br/>HITL gate - interrupt"}

    subgraph MEASURE["Measure and decide"]
        direction LR
        KP["fa:fa-chart-line kpi_analytics<br/>snapshots + aggregates"]
        RO["fa:fa-coins roi_calculator<br/>blended ROI + payback"]
        OP["fa:fa-arrows-rotate optimization<br/>up to 3 actions"]
        VR["fa:fa-scale-balanced was it worth it?<br/>ROI verdict"]
        KP --> RO --> OP --> VR
    end

    REJ["fa:fa-rotate-left rejected - revise with comments<br/>loops back to creative_strategist"]
    BL["fa:fa-ban blocked<br/>3 revisions reached"]

    PLAN --> HR
    HR -->|approved| MEASURE
    HR -->|rejected| REJ
    HR -->|3 revisions| BL

    classDef research fill:#E7ECF1,stroke:#3B4552,color:#12181F
    classDef creative fill:#F3F0FE,stroke:#6D28D9,color:#5B21B6
    classDef media fill:#FDF3E3,stroke:#B45309,color:#7C4A08
    classDef compliance fill:#FAF3DC,stroke:#A16207,color:#713F12
    classDef hitl fill:#E9F2ED,stroke:#176B4F,color:#11533E,stroke-width:2px
    classDef data fill:#E3EEF5,stroke:#38678C,color:#1F4E6E
    classDef money fill:#E9F2ED,stroke:#176B4F,color:#11533E
    classDef verdict fill:#D5E6DD,stroke:#11533E,color:#0B3A2B,stroke-width:2px
    classDef bad fill:#F9DEDC,stroke:#B42318,color:#7F1D1D
    classDef loop fill:#FDF3E3,stroke:#B45309,color:#7C4A08,stroke-dasharray:4 3
    class RS research
    class CR creative
    class MP media
    class CO compliance
    class HR hitl
    class KP data
    class RO money
    class OP data
    class VR verdict
    class BL bad
    class REJ loop
"""

_ARCHITECTURE = """flowchart TB
    subgraph REQ["Request path"]
        direction LR
        UI["fa:fa-gauge-high Streamlit dashboard<br/>:8501"]
        API["fa:fa-server FastAPI :8000<br/>tenant header - routing - persistence"]
        GRAPH["fa:fa-diagram-project LangGraph orchestrator<br/>7-agent graph - Postgres checkpointer"]
        LLM["fa:fa-brain Coarena LLM<br/>gpt-6-luna"]
        UI -->|HTTP| API --> GRAPH
        GRAPH -->|LLM calls| LLM
    end

    subgraph MCPLAYER["MCP server layer - eight servers, every write gated"]
        direction LR
        M1["fa:fa-magnifying-glass research<br/>:9001"]
        M2["fa:fa-bullhorn ads<br/>:9002"]
        M3["fa:fa-chart-line analytics<br/>:9003"]
        M4["fa:fa-coins roi<br/>:9004"]
        M5["fa:fa-shield-halved compliance<br/>:9005"]
        M6["fa:fa-bullseye google-ads<br/>:9006"]
        M7["fa:fa-earth-americas public_data<br/>:9007"]
        M8["fa:fa-handshake linkedin<br/>:9012"]
        M1 ~~~ M2 ~~~ M3 ~~~ M4 ~~~ M5 ~~~ M6 ~~~ M7 ~~~ M8
    end

    subgraph EXT["Free public data sources - no paid keys"]
        direction LR
        E1["fa:fa-cloud DuckDuckGo + Wikipedia<br/>Google Trends / Suggest"]
        E2["fa:fa-newspaper SEC EDGAR - HN - Reddit<br/>OpenCorporates - IPInfo"]
        E1 ~~~ E2
    end

    subgraph STORE["Storage"]
        direction LR
        PG[("fa:fa-database Postgres<br/>campaigns / KPIs / audit")]
        RD[("fa:fa-bolt Redis<br/>rate limits")]
        PG ~~~ RD
    end

    REQ -->|tool calls| MCPLAYER
    MCPLAYER -->|read-only APIs| EXT
    REQ -->|checkpoints + state| STORE

    classDef ui fill:#E7ECF1,stroke:#12181F,color:#12181F,stroke-width:1.5px
    classDef core fill:#E9F2ED,stroke:#176B4F,color:#11533E,stroke-width:2px
    classDef brain fill:#FDF3E3,stroke:#B45309,color:#7C4A08,stroke-width:1.5px
    classDef mcp fill:#F3F0FE,stroke:#6D28D9,color:#5B21B6,stroke-width:1.5px
    classDef external fill:#EEF1F4,stroke:#6B7683,color:#3B4552
    classDef store fill:#E3EEF5,stroke:#38678C,color:#1F4E6E,stroke-width:1.5px
    class UI,API ui
    class GRAPH core
    class LLM brain
    class M1,M2,M3,M4,M5,M6,M7,M8 mcp
    class E1,E2 external
    class PG,RD store
"""

_GUARDRAILS = """flowchart TB
    CALL["fa:fa-bolt Every agent call"]

    subgraph CHAIN["Guardrail chain - cannot be disabled in staging or prod"]
        direction LR
        PY["fa:fa-file-contract Pydantic<br/>typed contracts"]
        TS["fa:fa-key TenantScope<br/>tenant isolation"]
        RL["fa:fa-gauge-high Redis<br/>rate limiting"]
        AL["fa:fa-list-check Audit log<br/>append-only"]
        PE["fa:fa-scale-balanced PolicyEnforcer<br/>compliance rules"]
        CC["fa:fa-piggy-bank Cost ceiling<br/>200 LLM calls per campaign"]
        PY --> TS --> RL --> AL --> PE --> CC
    end

    CALL --> CHAIN
    CHAIN --> G{fa:fa-circle-check all gates pass?}
    G -->|yes| OK["fa:fa-circle-play Call executes<br/>recorded in agent_runs"]
    G -->|no| NO["fa:fa-ban Refused<br/>error + audit event"]

    classDef entry fill:#E7ECF1,stroke:#12181F,color:#12181F,stroke-width:1.5px
    classDef gate fill:#E9F2ED,stroke:#176B4F,color:#11533E
    classDef check fill:#FFFFFF,stroke:#3B4552,color:#12181F,stroke-width:2px
    classDef ok fill:#D5E6DD,stroke:#11533E,color:#0B3A2B,stroke-width:2px
    classDef bad fill:#F9DEDC,stroke:#B42318,color:#7F1D1D,stroke-width:2px
    class CALL entry
    class PY,TS,RL,AL,PE,CC gate
    class G check
    class OK ok
    class NO bad
"""

af_section("Agent pipeline", "The path every campaign travels, and the gate you approve at")
render_mermaid(_PIPELINE, 520)
af_legend(
    [
        ("Research", "#3B4552"),
        ("Creative", "#6D28D9"),
        ("Media", "#B45309"),
        ("Compliance", "#A16207"),
        ("HITL gate", "#176B4F"),
        ("Measure", "#38678C"),
        ("Verdict", "#11533E"),
        ("Blocked", "#B42318"),
    ]
)
af_caption(
    "The graph pauses at human_review via LangGraph interrupt(); approval issues an "
    "HMAC-signed token. Rejection loops back to creative_strategist with the reviewer "
    "comments - after 3 revisions the campaign is blocked."
)

af_section("System architecture", "Services and the MCP layer behind the console")
render_mermaid(_ARCHITECTURE, 500)
af_legend(
    [
        ("UI + API", "#12181F"),
        ("Orchestration", "#176B4F"),
        ("LLM", "#B45309"),
        ("MCP servers", "#6D28D9"),
        ("Public data", "#6B7683"),
        ("Storage", "#38678C"),
    ]
)
af_caption(
    "Streamlit calls FastAPI; the LangGraph orchestrator drives the MCP layer and the LLM. "
    "Ad-platform KPIs are deterministic mocks (is_mock) in dev until real credentials are "
    "configured: googleads/google-ads-mcp, Pipeboard remote MCPs (Meta / TikTok / Snap / "
    "Reddit) and the custom LinkedIn MCP."
)

af_section("Guardrails", "What every agent call must clear")
render_mermaid(_GUARDRAILS, 540)
af_legend(
    [
        ("Agent call", "#12181F"),
        ("Guardrail", "#176B4F"),
        ("Passes", "#11533E"),
        ("Refused", "#B42318"),
    ]
)
af_caption(
    "Every agent call passes through this chain - typed contracts, tenant isolation, rate "
    "limits, audit, compliance policy and the per-campaign LLM cost ceiling. "
    "Guardrails cannot be switched off in staging or prod."
)

st.markdown(
    '<div class="af-caption" style="margin-top:1.6rem;border-top:1px solid #D9DFE5;'
    'padding-top:0.9rem;">'
    "AdForge runs seven specialist agents over one campaign graph. KPI figures in dev "
    "are deterministic mocks (is_mock) until real ad-platform credentials are "
    "configured.</div>",
    unsafe_allow_html=True,
)
