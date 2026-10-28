"""Research MCP server — backs the Research agent.

Tools:
    web_search(query, max_results=5)         — multi-provider chain
    serp_scrape(query, top_n=10)             — Google SERP scrape via DuckDuckGo HTML
    wikipedia_search(query, n=3)             — Wikipedia public API (no key)
    keyword_clusters(seed_keyword, n=25)
    competitor_creatives(domain)

## Web search provider chain (v0.3+)

Previously this was Tavily-only, which required a paid API key. It now
tries providers in order, picking the first that returns results:

    1. DuckDuckGo HTML (duckduckgo.com/html) — FREE, no key, no auth
       Implements the same community pattern used by nickclyde/duckduckgo-mcp-server.
    2. Wikipedia public API (en.wikipedia.org/w/api.php) — FREE, no key
       Used as a secondary fallback for encyclopedic / definition queries.
    3. Tavily API (api.tavily.com) — paid, optional fallback when configured.

This means the Research agent works end-to-end with ZERO paid credentials.
Tavily is still supported as an upgrade path when higher-quality search
summaries are needed.

Each tool call is tenant-isolated via the X-AdForge-Tenant header — see
guardrails.data_isolation. The MCP server enforces this in the auth_check
middleware.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus

import httpx
from mcp.server.fastmcp import FastMCP

from adforge.config import get_settings
from adforge.mcp_servers.permissions import readonly

mcp = FastMCP("adforge-research")


def _check_tenant(headers: dict[str, str] | None) -> str:
    """Extract + validate tenant_id from request headers."""
    if not headers or "X-AdForge-Tenant" not in headers:
        raise ValueError("Missing X-AdForge-Tenant header — refusing to serve")
    return headers["X-AdForge-Tenant"]


# --------------------------------------------------------------------------- #
# Provider 1: DuckDuckGo HTML (free, no key)
# --------------------------------------------------------------------------- #


async def _duckduckgo_search(query: str, max_results: int) -> list[dict[str, str]]:
    """Scrape DuckDuckGo's HTML endpoint. Free, no API key, no auth.

    Implements the same pattern as nickclyde/duckduckgo-mcp-server.
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {
        "User-Agent": "AdForge/0.3 (research-agent; +https://github.com/your-org/adforge)"
    }
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        html = resp.text

    # DuckDuckGo HTML wraps each result in <a class="result__a" href="...">title</a>
    # and the snippet in <a class="result__snippet" href="...">snippet</a>.
    results = []
    # result__a links — capture URL + title
    link_pattern = re.compile(
        r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<a class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (raw_url, raw_title) in enumerate(links[:max_results]):
        # DuckDuckGo wraps URLs in a redirect like //duckduckgo.com/l/?uddg=<encoded>
        if "uddg=" in raw_url:
            m = re.search(r"uddg=([^&]+)", raw_url)
            if m:
                from urllib.parse import unquote
                actual_url = unquote(m.group(1))
            else:
                actual_url = raw_url
        else:
            actual_url = raw_url

        # Strip HTML tags from title + snippet
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""

        results.append({
            "title": title,
            "url": actual_url,
            "snippet": snippet[:300],
        })

    return results


# --------------------------------------------------------------------------- #
# Provider 2: Wikipedia public API (free, no key)
# --------------------------------------------------------------------------- #


async def _wikipedia_search(query: str, n: int) -> list[dict[str, str]]:
    """Search Wikipedia's public API. No API key required."""
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AdForge/0.3"}) as client:
        # First: opensearch to get title matches
        resp = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": query,
                "limit": n,
                "namespace": "0",
                "format": "json",
                "origin": "*",
            },
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if len(data) < 4:
            return []
        titles = data[1]
        urls = data[3]

        # Then: fetch extracts for each title
        results = []
        for title, url in zip(titles, urls):
            extract_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "extracts",
                    "titles": title,
                    "exintro": "true",
                    "explaintext": "true",
                    "format": "json",
                    "origin": "*",
                },
            )
            if extract_resp.status_code == 200:
                pages = extract_resp.json().get("query", {}).get("pages", {})
                extract = next(iter(pages.values())).get("extract", "")
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": extract[:300] if extract else "",
                })
            else:
                results.append({"title": title, "url": url, "snippet": ""})
        return results


# --------------------------------------------------------------------------- #
# Provider 3: Tavily (optional, paid)
# --------------------------------------------------------------------------- #


async def _tavily_search(query: str, max_results: int) -> dict[str, Any]:
    """Tavily API. Paid; only used when TAVILY_API_KEY is set."""
    s = get_settings()
    if not s.tavily_api_key:
        return {"error": "no_tavily_key"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": s.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            },
        )
        if resp.status_code != 200:
            return {"error": f"tavily_{resp.status_code}"}
        data = resp.json()
    results = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:300]}
        for r in data.get("results", [])
    ]
    return {"answer": data.get("answer", ""), "results": results}


# --------------------------------------------------------------------------- #
# Public MCP tool — tries DuckDuckGo → Wikipedia → Tavily in order
# --------------------------------------------------------------------------- #


@mcp.tool()
@readonly
async def web_search(query: str, max_results: int = 5, headers: dict[str, str] | None = None) -> str:
    """Search the web using a multi-provider chain. Returns JSON of results.

    Tries providers in this order:
      1. DuckDuckGo HTML endpoint — FREE, no API key, no auth.
      2. Wikipedia public API — FREE, no key. Good for encyclopedic queries.
      3. Tavily API — optional paid fallback when TAVILY_API_KEY is set.

    This means the Research agent works end-to-end with ZERO paid credentials.
    """
    _check_tenant(headers)

    # Try DuckDuckGo first — most general
    ddg_results = await _duckduckgo_search(query, max_results)
    if ddg_results:
        return json.dumps({
            "provider": "duckduckgo",
            "query": query,
            "results": ddg_results,
        })

    # Wikipedia fallback — good for definitional / encyclopedic queries
    wiki_results = await _wikipedia_search(query, max_results)
    if wiki_results:
        return json.dumps({
            "provider": "wikipedia",
            "query": query,
            "results": wiki_results,
        })

    # Tavily paid fallback
    tavily = await _tavily_search(query, max_results)
    if "error" not in tavily:
        return json.dumps({
            "provider": "tavily",
            "query": query,
            "results": tavily["results"],
            "answer": tavily.get("answer", ""),
        })

    # Final fallback: mock so the agent doesn't crash
    return json.dumps({
        "provider": "mock",
        "query": query,
        "results": [
            {
                "title": f"Mock result for {query!r}",
                "url": f"https://example.com/{i}",
                "snippet": "All free providers failed (rate-limited / network). Set TAVILY_API_KEY for higher quality.",
            }
            for i in range(max_results)
        ],
    })


@mcp.tool()
@readonly
async def wikipedia_search(query: str, n: int = 3, headers: dict[str, str] | None = None) -> str:
    """Search Wikipedia directly. Useful for definitions, market context, company backgrounds.

    Free, no API key. Uses the official Wikipedia action API.
    """
    _check_tenant(headers)
    results = await _wikipedia_search(query, n)
    return json.dumps({"query": query, "results": results, "count": len(results)})


@mcp.tool()
@readonly
async def serp_scrape(query: str, top_n: int = 10, headers: dict[str, str] | None = None) -> str:
    """Scrape top-N search results for a query. Currently delegates to web_search.

    v0.3 plan: implement a real SERP scraper using DuckDuckGo HTML +
    page-content fetch (via httpx + readability-lxml). For now, alias to
    web_search.
    """
    _check_tenant(headers)
    return await web_search(query, max_results=top_n, headers=headers)


@mcp.tool()
@readonly
async def keyword_clusters(seed_keyword: str, n: int = 25, headers: dict[str, str] | None = None) -> str:
    """Return n clustered keyword ideas for the seed keyword.

    v1: deterministic expansion using common adjectives + intents.
    Production version would call Google Keyword Planner via the official
    google-ads-mcp.
    """
    _check_tenant(headers)
    intents = ["buy", "best", "cheap", "review", "alternatives", "vs", "near me", "for small business"]
    qualifiers = ["premium", "professional", "enterprise", "free trial", "open source", "demo"]
    keywords = []
    for i in range(min(n, len(intents) * len(qualifiers))):
        intent = intents[i % len(intents)]
        qualifier = qualifiers[i // len(intents)]
        keywords.append(f"{intent} {seed_keyword} {qualifier}")
    return json.dumps({"seed": seed_keyword, "clusters": keywords})


@mcp.tool()
@readonly
async def competitor_creatives(domain: str, headers: dict[str, str] | None = None) -> str:
    """Scrape active ads from a competitor domain.

    v1: returns mock creatives. Production version would call the
    Facebook Ad Library API + Google Ads Transparency Center.
    """
    _check_tenant(headers)
    return json.dumps({
        "domain": domain,
        "creatives": [
            {
                "headline": f"Try {domain.split('.')[0].title()} today",
                "primary_text": "See why thousands of teams switched. Start free, no credit card required.",
                "cta": "Start free",
                "platform": "google",
            },
            {
                "headline": f"Stop wasting time on spreadsheets",
                "primary_text": f"{domain.split('.')[0].title()} integrates with everything you use.",
                "cta": "Book a demo",
                "platform": "linkedin",
            },
        ],
    })
