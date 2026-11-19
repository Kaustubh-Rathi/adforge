"""Research MCP server — backs the Research agent.

Tools:
    web_search(query, max_results=5)         — multi-provider chain
    serp_scrape(query, top_n=10)             — Google SERP scrape via DuckDuckGo HTML
    wikipedia_search(query, n=3)             — Wikipedia public API (no key)
    keyword_clusters(seed_keyword, n=25)
    competitor_creatives(domain)
    trends_interest(keyword, geo, timeframe) — Google Trends widget API (no key)
    fetch_page(url)                          — Jina Reader page-to-markdown (no key)

## Web search provider chain (v0.3+)

Previously this was Tavily-only, which required a paid API key. It now
tries providers in order, picking the first that returns results:

    1. DuckDuckGo HTML (duckduckgo.com/html) — FREE, no key, no auth
       Implements the same community pattern used by nickclyde/duckduckgo-mcp-server.
    2. DuckDuckGo Lite via Jina Reader (r.jina.ai -> lite.duckduckgo.com/lite)
       — FREE, no key. The Lite endpoint is plain HTML that survives the
       anomaly/challenge page that blocks the /html/ endpoint on some networks.
    3. Bing RSS endpoint (www.bing.com/search?format=rss) — FREE, no key.
       Machine-readable RSS 2.0 responses; Bing's HTML SERP is bot-blocked
       (and reading it via Jina returns junk), but the RSS format still works.
    4. Wikipedia public API (en.wikipedia.org/w/api.php) — FREE, no key
       Used as a secondary fallback for encyclopedic / definition queries.
    5. Tavily API (api.tavily.com) — paid, optional fallback when configured.

This means the Research agent works end-to-end with ZERO paid credentials.
Tavily is still supported as an upgrade path when higher-quality search
summaries are needed.

Each tool call is tenant-isolated via the X-AdForge-Tenant header — see
guardrails.data_isolation. The MCP server enforces this in the auth_check
middleware.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from adforge.config import get_settings
from adforge.mcp_servers.permissions import READ_ONLY, readonly

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
    s = get_settings()
    url = f"{s.research_search_url}?q={quote_plus(query)}"
    headers = {"User-Agent": s.external_user_agent}
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, headers=headers, trust_env=False
    ) as client:
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

        results.append(
            {
                "title": title,
                "url": actual_url,
                "snippet": snippet[:300],
            }
        )

    return results


# --------------------------------------------------------------------------- #
# Provider 1b: DuckDuckGo Lite via Jina Reader (free, no key)
# --------------------------------------------------------------------------- #


def _strip_inline_markup(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"[*_`]", "", text)
    return text.strip()


# A numbered result line in a Jina-rendered DDG Lite page looks like:
#   1.[Title text](https://duckduckgo.com/l/?uddg=<encoded>&rut=<hash>)
_DDG_LITE_LINK_RE = re.compile(r"^\d+\.\[(.+)\]\((.+)\)$")
_DDG_LITE_SKIP_PREFIXES = ("URL Source:", "Title:", "Markdown Content:")


def _resolve_ddg_lite_url(url: str) -> str:
    """Unwrap DuckDuckGo's /l/?uddg=<encoded> redirect links.

    Keeps the raw URL when it isn't a uddg redirect (or can't be parsed).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.netloc.lower() not in ("duckduckgo.com", "www.duckduckgo.com"):
        return url
    if not parsed.path.startswith("/l/"):
        return url
    target = parse_qs(parsed.query).get("uddg")
    if target and target[0]:
        # parse_qs already percent-decodes the value.
        return target[0]
    return url


def _parse_ddg_lite_markdown(markdown: str, max_results: int) -> list[dict[str, str]]:
    """Extract results from a Jina-converted DuckDuckGo Lite SERP.

    Each numbered ``[title](url)`` line starts a result; the following
    non-empty lines (until the next numbered link or a Jina header line)
    are joined as its snippet.
    """
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    snippet_parts: list[str] = []

    def flush() -> None:
        if current is None:
            return
        current["snippet"] = " ".join(snippet_parts)[:300]
        if current["title"] or current["url"]:
            results.append(current)

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        m = _DDG_LITE_LINK_RE.match(line)
        if m:
            flush()
            if len(results) >= max_results:
                break
            title = _strip_inline_markup(m.group(1))
            url = _resolve_ddg_lite_url(m.group(2))
            current = {"title": title, "url": url, "snippet": ""}
            snippet_parts = []
            continue
        if current is None or not line:
            continue
        if line.startswith(_DDG_LITE_SKIP_PREFIXES):
            continue
        snippet_parts.append(_strip_inline_markup(line))
    flush()
    return results[:max_results]


async def _ddg_lite_search(query: str, max_results: int) -> list[dict[str, str]]:
    """Fetch DuckDuckGo Lite results through Jina Reader. Keyless.

    Used when the DuckDuckGo /html/ endpoint serves its anomaly/challenge
    page. The /lite/ endpoint is a minimal HTML page Jina renders cleanly
    into numbered markdown links.
    """
    s = get_settings()
    url = f"{s.jina_reader_url.rstrip('/')}/https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
    headers = {"User-Agent": s.external_user_agent}
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, headers=headers, trust_env=False
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        markdown = resp.text
    return _parse_ddg_lite_markdown(markdown, max_results)


# --------------------------------------------------------------------------- #
# Provider 1c: Bing RSS endpoint (free, no key)
# --------------------------------------------------------------------------- #

# Bing's HTML SERP (and its Jina-Reader rendering) is bot-blocked, but the
# RSS format endpoint still serves real results to a browser User-Agent.
_BING_RSS_URL = "https://www.bing.com/search"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def _parse_bing_rss(raw: str, max_results: int) -> list[dict[str, str]]:
    """Parse Bing's RSS 2.0 search response into result dicts.

    Never raises — a malformed body yields an empty list.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    results: list[dict[str, str]] = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        title = (title_el.text or "").strip() if title_el is not None else ""
        url = (link_el.text or "").strip() if link_el is not None else ""
        if not url:
            continue
        snippet = ""
        if desc_el is not None and desc_el.text:
            snippet = re.sub(r"<[^>]+>", " ", html_lib.unescape(desc_el.text))
            snippet = re.sub(r"\s+", " ", snippet).strip()[:300]
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results[:max_results]


async def _bing_rss_search(query: str, max_results: int) -> list[dict[str, str]]:
    """Fetch Bing's RSS search endpoint directly. Keyless."""
    s = get_settings()
    headers = {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, headers=headers, trust_env=False
    ) as client:
        resp = await client.get(_BING_RSS_URL, params={"q": query, "format": "rss"})
        if resp.status_code != 200:
            return []
        raw = resp.text
    return _parse_bing_rss(raw, max_results)


# --------------------------------------------------------------------------- #
# Provider 2: Wikipedia public API (free, no key)
# --------------------------------------------------------------------------- #


async def _wikipedia_search(query: str, n: int) -> list[dict[str, str]]:
    """Search Wikipedia's public API. No API key required."""
    s = get_settings()
    headers = {"User-Agent": s.external_user_agent}
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, headers=headers, trust_env=False
    ) as client:
        # First: opensearch to get title matches
        resp = await client.get(
            s.wikipedia_api_url,
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
        for title, url in zip(titles, urls, strict=False):
            extract_resp = await client.get(
                s.wikipedia_api_url,
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
                results.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": extract[:300] if extract else "",
                    }
                )
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
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.post(
            f"{s.tavily_base_url.rstrip('/')}/search",
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
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "")[:300],
        }
        for r in data.get("results", [])
    ]
    return {"answer": data.get("answer", ""), "results": results}


# --------------------------------------------------------------------------- #
# Public MCP tool — tries DuckDuckGo → DDG Lite → Bing RSS → Wikipedia → Tavily
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ_ONLY)
@readonly
async def web_search(
    query: str, max_results: int = 5, headers: dict[str, str] | None = None
) -> str:
    """Search the web using a multi-provider chain. Returns JSON of results.

    Tries providers in this order:
      1. DuckDuckGo HTML endpoint — FREE, no API key, no auth.
      2. DuckDuckGo Lite via Jina Reader — FREE, no key. Covers networks
         where the DuckDuckGo HTML endpoint serves an anomaly/challenge page.
      3. Bing RSS endpoint — FREE, no key. Machine-readable results page that
         avoids Bing's HTML bot-block.
      4. Wikipedia public API — FREE, no key. Good for encyclopedic queries.
      5. Tavily API — optional paid fallback when TAVILY_API_KEY is set.

    This means the Research agent works end-to-end with ZERO paid credentials.
    """
    _check_tenant(headers)

    # Try DuckDuckGo first — most general
    ddg_results = await _duckduckgo_search(query, max_results)
    if ddg_results:
        return json.dumps(
            {
                "provider": "duckduckgo",
                "query": query,
                "results": ddg_results,
            }
        )

    # DuckDuckGo Lite via Jina Reader — DuckDuckGo answers with an anomaly
    # page (HTTP 200, zero parsed results) on some networks.
    lite_results = await _ddg_lite_search(query, max_results)
    if lite_results:
        return json.dumps(
            {
                "provider": "ddg_lite",
                "query": query,
                "results": lite_results,
            }
        )

    # Bing RSS endpoint — Bing's HTML SERP is bot-blocked but format=rss
    # still returns real, machine-readable results.
    rss_results = await _bing_rss_search(query, max_results)
    if rss_results:
        return json.dumps(
            {
                "provider": "bing_rss",
                "query": query,
                "results": rss_results,
            }
        )

    # Wikipedia fallback — good for definitional / encyclopedic queries
    wiki_results = await _wikipedia_search(query, max_results)
    if wiki_results:
        return json.dumps(
            {
                "provider": "wikipedia",
                "query": query,
                "results": wiki_results,
            }
        )

    # Tavily paid fallback
    tavily = await _tavily_search(query, max_results)
    if "error" not in tavily:
        return json.dumps(
            {
                "provider": "tavily",
                "query": query,
                "results": tavily["results"],
                "answer": tavily.get("answer", ""),
            }
        )

    return json.dumps(
        {
            "provider": "none",
            "query": query,
            "results": [],
            "error": "all search providers failed; no simulated results returned",
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def wikipedia_search(query: str, n: int = 3, headers: dict[str, str] | None = None) -> str:
    """Search Wikipedia directly. Useful for definitions, market context, company backgrounds.

    Free, no API key. Uses the official Wikipedia action API.
    """
    _check_tenant(headers)
    results = await _wikipedia_search(query, n)
    return json.dumps({"query": query, "results": results, "count": len(results)})


@mcp.tool(annotations=READ_ONLY)
@readonly
async def serp_scrape(query: str, top_n: int = 10, headers: dict[str, str] | None = None) -> str:
    """Scrape top-N search results for a query. Currently delegates to web_search.

    v0.3 plan: implement a real SERP scraper using DuckDuckGo HTML +
    page-content fetch (via httpx + readability-lxml). For now, alias to
    web_search.
    """
    _check_tenant(headers)
    result: str = await web_search(query, max_results=top_n, headers=headers)
    return result


async def _google_suggest(query: str, n: int) -> list[str]:
    """Google's public autocomplete endpoint — real suggestions, no key."""
    s = get_settings()
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        resp = await client.get(
            s.google_suggest_url,
            params={"client": "firefox", "q": query},
        )
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    suggestions = data[1] if isinstance(data, list) and len(data) > 1 else []
    return [str(s).strip() for s in suggestions if str(s).strip()][:n]


@mcp.tool(annotations=READ_ONLY)
@readonly
async def keyword_clusters(
    seed_keyword: str, n: int = 25, headers: dict[str, str] | None = None
) -> str:
    """Return real keyword suggestions for a seed keyword.

    Uses Google's public autocomplete endpoint. For search-volume and
    competition metrics, connect Google Ads Keyword Planner credentials.
    """
    _check_tenant(headers)
    n = max(1, min(n, 100))
    keywords = await _google_suggest(seed_keyword, n)
    if not keywords:
        return json.dumps(
            {
                "seed": seed_keyword,
                "clusters": [],
                "error": "keyword suggestions unavailable; no simulated keywords returned",
            }
        )
    return json.dumps({"seed": seed_keyword, "clusters": keywords, "source": "google_suggest"})


@mcp.tool(annotations=READ_ONLY)
@readonly
async def competitor_creatives(domain: str, headers: dict[str, str] | None = None) -> str:
    """Fetch active ads for a competitor from Meta's Ad Library API.

    Requires ``META_AD_LIBRARY_TOKEN``. Without it the tool returns an explicit
    configuration error rather than fabricated creatives.
    """
    _check_tenant(headers)
    s = get_settings()
    if not s.meta_ad_library_token:
        return json.dumps(
            {
                "domain": domain,
                "creatives": [],
                "error": "meta_ad_library_token_not_configured",
            }
        )
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.meta_graph_base.rstrip('/')}/{s.meta_graph_api_version}/ads_archive",
            params={
                "access_token": s.meta_ad_library_token,
                "ad_reached_countries": '["US"]',
                "search_terms": domain,
                "ad_active_status": "ACTIVE",
                "fields": "id,ad_creative_bodies,ad_creative_link_captions,ad_creative_link_titles",
                "limit": 25,
            },
        )
    if resp.status_code >= 400:
        return json.dumps(
            {
                "domain": domain,
                "creatives": [],
                "error": f"meta_ad_library_http_{resp.status_code}",
                "detail": resp.text[:300],
            }
        )
    body = resp.json()
    creatives = []
    for ad in body.get("data", []):
        creatives.append(
            {
                "id": ad.get("id"),
                "headline": (ad.get("ad_creative_link_titles") or [""])[0],
                "primary_text": (ad.get("ad_creative_bodies") or [""])[0],
                "caption": (ad.get("ad_creative_link_captions") or [""])[0],
                "platform": "meta",
            }
        )
    return json.dumps({"domain": domain, "creatives": creatives, "count": len(creatives)})


# --------------------------------------------------------------------------- #
# Google Trends widget API (free, no key) + Jina Reader (free, no key)
# --------------------------------------------------------------------------- #


def _strip_trends_prefix(body: str) -> str:
    """Remove the ``)]}'`` anti-JSON-hijacking prefix Google Trends prepends."""
    if body.startswith(")]}'"):
        return body[4:].lstrip()
    return body


@mcp.tool(annotations=READ_ONLY)
@readonly
async def trends_interest(
    keyword: str,
    geo: str = "US",
    timeframe: str = "today 12-m",
    headers: dict[str, str] | None = None,
) -> str:
    """Return real Google Trends interest-over-time series for a keyword.

    Uses the unofficial Trends widget API (2-step explore -> multiline
    exchange). No API key, but Google can rate-limit or block datacenter
    IPs; on any failure the tool returns an explicit error rather than a
    fabricated series.
    """
    _check_tenant(headers)
    s = get_settings()
    base = s.google_trends_base_url.rstrip("/")
    comparison_item = [{"keyword": keyword, "geo": geo, "time": timeframe}]

    async def _error(payload: dict[str, Any]) -> str:
        payload.update({"keyword": keyword, "geo": geo, "timeframe": timeframe})
        return json.dumps(payload)

    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        # Warm-up so the client carries the cookies the widget endpoints require.
        await client.get(f"{base}/trends/")

        explore = await client.get(
            f"{base}/trends/api/explore",
            params={
                "hl": "en-US",
                "tz": 0,
                "req": json.dumps(
                    {"comparisonItem": comparison_item, "category": 0, "property": ""}
                ),
            },
        )
        if explore.status_code != 200:
            return await _error(
                {
                    "error": f"google_trends_http_{explore.status_code}",
                    "detail": explore.text[:300],
                }
            )
        try:
            explore_body = json.loads(_strip_trends_prefix(explore.text))
        except ValueError:
            return await _error({"error": "google_trends_explore_unparsable"})

        widgets = explore_body.get("widgets") or []
        token = widgets[0].get("token") if widgets else None
        if not token:
            return await _error({"error": "google_trends_widget_unavailable"})

        multiline = await client.get(
            f"{base}/trends/api/widgetdata/multiline",
            params={
                "hl": "en-US",
                "tz": 0,
                "token": token,
                "req": json.dumps(
                    {
                        "time": timeframe,
                        "resolution": "WEEK",
                        "locale": "en-US",
                        "comparisonItem": comparison_item,
                        "requestOptions": {"property": "", "backend": "IZG", "category": 0},
                    }
                ),
            },
        )
        if multiline.status_code != 200:
            return await _error(
                {
                    "error": f"google_trends_http_{multiline.status_code}",
                    "detail": multiline.text[:300],
                }
            )
        try:
            data = json.loads(_strip_trends_prefix(multiline.text))
        except ValueError:
            return await _error({"error": "google_trends_multiline_unparsable"})

    timeline = (data.get("default") or {}).get("timelineData") or []
    points = [
        {"date": entry.get("formattedTime"), "value": int(entry["value"][0])}
        for entry in timeline
        if entry.get("value")
    ]
    if not points:
        return await _error({"error": "google_trends_no_timeline_data", "points": []})
    average = round(sum(p["value"] for p in points) / len(points), 1)
    return json.dumps(
        {
            "keyword": keyword,
            "geo": geo,
            "timeframe": timeframe,
            "points": points,
            "average": average,
            "source": "google_trends",
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def fetch_page(url: str, headers: dict[str, str] | None = None) -> str:
    """Fetch a web page as readable markdown via Jina Reader (keyless tier).

    Returns the page text, truncated to ``agent_max_tool_result_chars``.
    On a non-200 response an explicit JSON error is returned instead.
    """
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        resp = await client.get(f"{s.jina_reader_url.rstrip('/')}/{url}")
    if resp.status_code != 200:
        return json.dumps(
            {
                "url": url,
                "error": f"jina_reader_http_{resp.status_code}",
                "detail": resp.text[:200],
            }
        )
    return resp.text[: s.agent_max_tool_result_chars]
