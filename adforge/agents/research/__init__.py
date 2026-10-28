"""Research Agent — market, competitor, and audience research.

Tools (via MCP):
    - web_search         (Tavily-backed AdForge research MCP)
    - serp_scrape        (AdForge research MCP)
    - keyword_clusters   (AdForge research MCP)
    - competitor_creatives (AdForge research MCP)
"""
from adforge.agents.research.agent import ResearchAgent

__all__ = ["ResearchAgent"]
