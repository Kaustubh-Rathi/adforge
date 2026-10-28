"""AdForge Streamlit dashboard."""
import streamlit as st

st.set_page_config(
    page_title="AdForge",
    page_icon=":money_with_wings:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Landing page — multi-page nav handled via adforge/dashboard/pages/*.py
st.title("AdForge")
st.subheader("End-to-end multi-agent ad campaign platform")

st.markdown(
    """
    Welcome to **AdForge**. Use the navigation in the sidebar to:

    - **Campaigns** — view, create, and run campaigns through the agent graph.
    - **Agent Traces** — inspect every agent's input, output, and usage.
    - **ROI Calculator** — see the bottom-line verdict for every live campaign.

    ## Architecture at a glance

    ```
    ┌─────────────────────────────────────────────────────────┐
    │                       Streamlit dashboard                │
    └───────────────────────┬─────────────────────────────────┘
                            │ HTTP
    ┌───────────────────────▼─────────────────────────────────┐
    │                     FastAPI (port 8000)                  │
    └───────────────────────┬─────────────────────────────────┘
                            │
    ┌───────────────────────▼─────────────────────────────────┐
    │              LangGraph orchestrator (7 agents)          │
    │  research → creative → media → compliance → kpi →       │
    │  roi → optimization                                     │
    └──┬─────────┬──────────┬──────────┬──────────┬───────────┘
       │         │          │          │          │
       ▼         ▼          ▼          ▼          ▼
    ┌──────┐ ┌──────┐ ┌──────────┐ ┌──────┐ ┌─────────────┐
    │research│ │ ads  │ │analytics │ │ roi  │ │ compliance  │
    │ MCP   │ │ MCP  │ │ MCP      │ │ MCP  │ │ MCP         │
    └──────┘ └──────┘ └──────────┘ └──────┘ └─────────────┘
       │         │                     │
       │         ├──── googleads/google-ads-mcp (official)
       │         ├──── pipeboard-co MCP family (Meta/TikTok/Snap/Reddit)
       │         └──── adforge/mcp_servers/ads/linkedin.py (custom)
       │
       └──── Tavily web search
    ```
    """
)
