"""Public data MCP server — aggregates 10+ free / freemium data sources.

Tools exposed (all @readonly, all free / no-auth-where-possible):
    similarweb_traffic(domain)               — SimilarWeb (free tier via Composio Rube)
    appstore_search(query, country='us')    — Apple iTunes public API (no key)
    appstore_reviews(app_id, country='us')   — Apple RSS public API (no key)
    ipinfo_lookup(ip)                       — IPInfo (50k/month free)
    abuseipdb_check(ip)                     — AbuseIPDB (1k/day free)
    github_repo_stats(repo)                 — GitHub public API (no key, 60/hr/IP)
    hackernews_top(story_type='top', n=10)  — HackerNews Firebase API (no key, unlimited)
    reddit_search(query, subreddit='all', n=10) — Reddit JSON API (no key, read-only)
    stackexchange_search(query, site='stackoverflow', n=10) — Stack Exchange API (no key, free)
    producthunt_search(query, n=10)         — Product Hunt unofficial search
    opencorporates_search(companies, n=10)  — OpenCorporates API (free tier, no key)
    wikipedia_search_direct(query, n=3)     — Wikipedia (no key) — duplicate of research MCP's
                                              but useful when called from non-research agents
    sec_financials(ticker_or_cik, metric, ...) — SEC EDGAR XBRL company-concept API (no key,
                                              requires declarative User-Agent)
    wikipedia_pageviews(article, start, end, ...) — Wikimedia REST pageviews API (no key)

These tools give the Research agent REAL public data without paid ad-platform
credentials. When keys ARE configured (env vars), they're used; otherwise
free-tier / no-auth endpoints are used.

External MCP repos referenced (see docs/RESEARCH.md):
    - meyusufdemirci/app-store-operator
    - NimbleBrainInc/mcp-ipinfo
    - AbuseIPDB community MCP
    - SimilarWeb skill via Composio Rube MCP
    - nickclyde/duckduckgo-mcp-server (DuckDuckGo search pattern)
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

from adforge.config import get_settings
from adforge.mcp_servers.permissions import READ_ONLY, readonly

mcp = FastMCP("adforge-public-data")


def _check_tenant(headers: dict[str, str] | None) -> str:
    if not headers or "X-AdForge-Tenant" not in headers:
        raise ValueError("Missing X-AdForge-Tenant header — refusing to serve")
    return headers["X-AdForge-Tenant"]


@mcp.tool(annotations=READ_ONLY)
@readonly
async def similarweb_traffic(domain: str, headers: dict[str, str] | None = None) -> str:
    """Get website traffic + engagement stats for a domain.

    Requires ``SIMILARWEB_API_KEY``; without it the tool returns an explicit
    configuration error rather than fabricated traffic numbers.
    """
    _check_tenant(headers)
    s = get_settings()
    if not s.similarweb_api_key:
        return json.dumps(
            {
                "domain": domain,
                "error": "similarweb_not_configured",
                "detail": "Set SIMILARWEB_API_KEY to enable traffic data.",
            }
        )
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.similarweb_api_base.rstrip('/')}/v1/website/{domain}"
            "/total-traffic-and-engagement/visits",
            headers={"Authorization": f"Bearer {s.similarweb_api_key}"},
            params={"granularity": "monthly", "main_domain_only": "true"},
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"SimilarWeb API {resp.status_code}: {resp.text[:200]}"})
        data = resp.json()
    return json.dumps({"domain": domain, "raw": data})


@mcp.tool(annotations=READ_ONLY)
@readonly
async def appstore_search(
    query: str, country: str = "us", headers: dict[str, str] | None = None
) -> str:
    """Search the Apple App Store. Mirrors meyusufdemirci/app-store-operator.

    Uses Apple's public search endpoint — no API key required.
    """
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.itunes_api_base.rstrip('/')}/search",
            params={"term": query, "country": country, "media": "software", "limit": 10},
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"iTunes API {resp.status_code}"})
        data = resp.json()
    results = [
        {
            "track_id": r.get("trackId"),
            "track_name": r.get("trackName"),
            "seller_name": r.get("sellerName"),
            "primary_genre": r.get("primaryGenreName"),
            "user_rating": r.get("averageUserRating"),
            "user_rating_count": r.get("userRatingCount"),
            "price": r.get("price", 0),
            "url": r.get("trackViewUrl"),
        }
        for r in data.get("results", [])
    ]
    return json.dumps({"query": query, "country": country, "apps": results})


@mcp.tool(annotations=READ_ONLY)
@readonly
async def appstore_reviews(
    app_id: int, country: str = "us", limit: int = 20, headers: dict[str, str] | None = None
) -> str:
    """Fetch recent App Store reviews for an app. Public RSS endpoint."""
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.itunes_api_base.rstrip('/')}/{country}/rss/customerreviews"
            f"/id={app_id}/sort=mostrecent/json",
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"iTunes RSS {resp.status_code}"})
        data = resp.json()
    entries = data.get("feed", {}).get("entry", [])
    # First entry is the app metadata, skip it.
    reviews = []
    for entry in entries[1 : limit + 1]:
        reviews.append(
            {
                "rating": int(entry.get("im:rating", {}).get("label", 0)),
                "title": entry.get("title", {}).get("label", ""),
                "content": entry.get("content", {}).get("label", "")[:500],
                "author": entry.get("author", {}).get("name", {}).get("label", ""),
                "version": entry.get("im:version", {}).get("label", ""),
            }
        )
    return json.dumps(
        {"app_id": app_id, "country": country, "reviews": reviews, "count": len(reviews)}
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def ipinfo_lookup(ip: str, headers: dict[str, str] | None = None) -> str:
    """Look up IP geolocation + ASN. Uses ipinfo.io — 50k/month free tier."""
    _check_tenant(headers)
    s = get_settings()
    token = s.ipinfo_api_token
    url = f"{s.ipinfo_api_base.rstrip('/')}/{ip}/json"
    params = {"token": token} if token else {}
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return json.dumps({"error": f"ipinfo API {resp.status_code}"})
        data = resp.json()
    return json.dumps(
        {
            "ip": ip,
            "city": data.get("city"),
            "region": data.get("region"),
            "country": data.get("country"),
            "org": data.get("org"),  # e.g. "AS15169 Google LLC"
            "hostname": data.get("hostname"),
            "loc": data.get("loc"),  # "lat,lng"
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def abuseipdb_check(ip: str, headers: dict[str, str] | None = None) -> str:
    """Check IP reputation via AbuseIPDB. Free tier: 1000 checks/day."""
    _check_tenant(headers)
    s = get_settings()
    if not s.abuseipdb_api_key:
        return json.dumps(
            {
                "ip": ip,
                "error": "abuseipdb_not_configured",
                "detail": "Set ABUSEIPDB_API_KEY to enable IP reputation checks.",
            }
        )
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.abuseipdb_api_base.rstrip('/')}/check",
            headers={"Key": s.abuseipdb_api_key, "Accept": "application/json"},
            params={"ipAddress": ip, "maxAgeInDays": 90},
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"AbuseIPDB {resp.status_code}"})
        data = resp.json().get("data", {})
    return json.dumps(
        {
            "ip": ip,
            "abuse_confidence_score": data.get("abuseConfidenceScore", 0),
            "total_reports": data.get("totalReports", 0),
            "country": data.get("countryCode"),
            "usage_type": data.get("usageType"),
            "isp": data.get("isp"),
            "domain": data.get("domain"),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def github_repo_stats(repo: str, headers: dict[str, str] | None = None) -> str:
    """Fetch GitHub repo stats (stars, forks, open issues). Public API, no auth.

    NOTE: 60 req/hour per IP without auth. Set GITHUB_TOKEN to raise to 5000/hr.
    """
    _check_tenant(headers)
    s = get_settings()
    gh_headers = {
        "User-Agent": s.external_user_agent,
        "Accept": "application/vnd.github+json",
    }
    if s.github_token:
        gh_headers["Authorization"] = f"Bearer {s.github_token}"
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, headers=gh_headers, trust_env=False
    ) as client:
        resp = await client.get(f"{s.github_api_base.rstrip('/')}/repos/{repo}")
        if resp.status_code != 200:
            return json.dumps({"error": f"GitHub API {resp.status_code}: {resp.text[:200]}"})
        data = resp.json()
    return json.dumps(
        {
            "repo": repo,
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "language": data.get("language"),
            "license": data.get("license", {}).get("name") if data.get("license") else None,
            "last_push": data.get("pushed_at"),
            "description": data.get("description"),
        }
    )


# --------------------------------------------------------------------------- #
# v0.3: New free public-data tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ_ONLY)
@readonly
async def hackernews_top(
    story_type: str = "top", n: int = 10, headers: dict[str, str] | None = None
) -> str:
    """Fetch top Hacker News stories. Firebase API, no auth, unlimited rate.

    story_type: top | new | best | ask | show | job
    Useful for trend detection — what's the dev/early-adopter crowd talking about?
    """
    _check_tenant(headers)
    s = get_settings()
    if story_type not in ("top", "new", "best", "ask", "show", "job"):
        return json.dumps({"error": "story_type must be one of: top, new, best, ask, show, job"})

    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        ids_resp = await client.get(f"{s.hackernews_api_base.rstrip('/')}/{story_type}stories.json")
        if ids_resp.status_code != 200:
            return json.dumps({"error": f"HN API {ids_resp.status_code}"})
        ids = ids_resp.json()[:n]

        stories = []
        for story_id in ids:
            story_resp = await client.get(
                f"{s.hackernews_api_base.rstrip('/')}/item/{story_id}.json"
            )
            if story_resp.status_code == 200:
                s_data = story_resp.json()
                if s_data:
                    stories.append(
                        {
                            "id": s_data.get("id"),
                            "title": s_data.get("title"),
                            "url": s_data.get(
                                "url", f"https://news.ycombinator.com/item?id={story_id}"
                            ),
                            "score": s_data.get("score", 0),
                            "comments": s_data.get("descendants", 0),
                            "by": s_data.get("by"),
                            "time": s_data.get("time"),
                        }
                    )
        return json.dumps(
            {
                "story_type": story_type,
                "stories": stories,
                "count": len(stories),
            }
        )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def reddit_search(
    query: str,
    subreddit: str = "all",
    n: int = 10,
    sort: str = "relevance",
    headers: dict[str, str] | None = None,
) -> str:
    """Search Reddit via the public JSON API. No auth required (read-only).

    Useful for finding organic discussion about a product / competitor.
    Sort: relevance | hot | top | new | comments
    """
    _check_tenant(headers)
    s = get_settings()
    if sort not in ("relevance", "hot", "top", "new", "comments"):
        return json.dumps({"error": "sort must be one of: relevance, hot, top, new, comments"})

    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        resp = await client.get(
            f"{s.reddit_api_base.rstrip('/')}/r/{subreddit}/search.json",
            params={
                "q": query,
                "sort": sort,
                "limit": n,
                "restrict_sr": "true" if subreddit != "all" else "false",
                "t": "year",
            },
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"Reddit API {resp.status_code}: {resp.text[:200]}"})
        data = resp.json()

    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        posts.append(
            {
                "title": p.get("title"),
                "url": p.get("url"),
                "permalink": f"https://reddit.com{p.get('permalink', '')}",
                "subreddit": p.get("subreddit"),
                "score": p.get("score", 0),
                "comments": p.get("num_comments", 0),
                "upvote_ratio": p.get("upvote_ratio"),
                "author": p.get("author"),
                "created_utc": p.get("created_utc"),
                "selftext": (p.get("selftext", "") or "")[:500],
            }
        )
    return json.dumps(
        {
            "query": query,
            "subreddit": subreddit,
            "sort": sort,
            "posts": posts,
            "count": len(posts),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def stackexchange_search(
    query: str,
    site: str = "stackoverflow",
    n: int = 10,
    headers: dict[str, str] | None = None,
) -> str:
    """Search the Stack Exchange network. No auth required.

    Useful for technical-product research (e.g. "How do developers feel about X API?").
    site: stackoverflow | serverfault | superuser | askubuntu | math | ...
    Full list: https://api.stackexchange.com/docs/sites
    """
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.stackexchange_api_base.rstrip('/')}/search",
            params={
                "order": "desc",
                "sort": "relevance",
                "intitle": query,
                "site": site,
                "pagesize": n,
            },
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"StackExchange API {resp.status_code}"})
        data = resp.json()

    items = []
    for item in data.get("items", [])[:n]:
        items.append(
            {
                "title": item.get("title"),
                "link": item.get("link"),
                "score": item.get("score", 0),
                "answers": item.get("answer_count", 0),
                "is_answered": item.get("is_answered"),
                "tags": item.get("tags", []),
                "view_count": item.get("view_count", 0),
                "creation_date": item.get("creation_date"),
            }
        )
    return json.dumps(
        {
            "query": query,
            "site": site,
            "items": items,
            "count": len(items),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def producthunt_search(query: str, n: int = 10, headers: dict[str, str] | None = None) -> str:
    """Search Product Hunt via the official GraphQL API.

    Requires ``PRODUCTHUNT_API_TOKEN``; without it the tool returns an explicit
    configuration error rather than querying a different provider.
    """
    _check_tenant(headers)
    s = get_settings()
    if not s.producthunt_api_token:
        return json.dumps(
            {
                "query": query,
                "error": "producthunt_not_configured",
                "detail": "Set PRODUCTHUNT_API_TOKEN to enable Product Hunt search.",
            }
        )

    graphql = """
        query SearchPosts($query: String!, $first: Int!) {
          posts(first: $first, order: RANKING, search: $query) {
            edges {
              node {
                id
                name
                tagline
                url
                votesCount
                commentsCount
                createdAt
              }
            }
          }
        }
    """
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.post(
            s.producthunt_api_url,
            headers={
                "Authorization": f"Bearer {s.producthunt_api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"query": graphql, "variables": {"query": query, "first": n}},
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"Product Hunt API {resp.status_code}: {resp.text[:200]}"})
        body = resp.json()

    errors = body.get("errors")
    if errors:
        return json.dumps({"error": "producthunt_graphql_error", "detail": errors})
    edges = (body.get("data") or {}).get("posts", {}).get("edges", [])
    hits = []
    for edge in edges[:n]:
        node = edge.get("node") or {}
        hits.append(
            {
                "id": node.get("id"),
                "title": node.get("name"),
                "tagline": node.get("tagline"),
                "url": node.get("url"),
                "points": node.get("votesCount", 0),
                "comments": node.get("commentsCount", 0),
                "created_at": node.get("createdAt"),
            }
        )
    return json.dumps(
        {
            "query": query,
            "source": "producthunt",
            "hits": hits,
            "count": len(hits),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def opencorporates_search(
    query: str, n: int = 10, headers: dict[str, str] | None = None
) -> str:
    """Search OpenCorporates — global company registry. Free tier (no key, 100/day).

    Useful for B2B prospecting: "Is competitor X a real registered entity?
    Where are they incorporated? Who are the directors?"
    """
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.external_http_timeout, trust_env=False) as client:
        resp = await client.get(
            f"{s.opencorporates_api_base.rstrip('/')}/companies/search",
            params={"q": query, "per_page": min(n, 30)},
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"OpenCorporates API {resp.status_code}"})
        data = resp.json()

    results = []
    for company in data.get("results", {}).get("companies", [])[:n]:
        c = company.get("company", {})
        results.append(
            {
                "name": c.get("name"),
                "jurisdiction": c.get("jurisdiction_code"),
                "company_number": c.get("company_number"),
                "incorporation_date": c.get("incorporation_date"),
                "status": c.get("current_status"),
                "address": c.get("registered_address_in_full"),
                "opencorporates_url": c.get("opencorporates_url"),
            }
        )
    return json.dumps(
        {
            "query": query,
            "companies": results,
            "count": len(results),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def wikipedia_search_direct(
    query: str, n: int = 3, headers: dict[str, str] | None = None
) -> str:
    """Search Wikipedia directly. Useful when called from non-research agents
    (e.g. Compliance agent verifying a company's regulatory history).

    Free, no API key. Same backend as research.wikipedia_search.
    """
    _check_tenant(headers)
    s = get_settings()
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
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
            return json.dumps({"error": f"Wikipedia API {resp.status_code}"})
        data = resp.json()
    if len(data) < 4:
        return json.dumps({"query": query, "results": []})
    titles, urls = data[1], data[3]
    results = [{"title": t, "url": u} for t, u in zip(titles, urls, strict=False)]
    return json.dumps({"query": query, "results": results, "count": len(results)})


# --------------------------------------------------------------------------- #
# v0.4: Keyless structured-data tools (SEC EDGAR XBRL + Wikimedia REST)
# --------------------------------------------------------------------------- #

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"


@mcp.tool(annotations=READ_ONLY)
@readonly
async def sec_financials(
    ticker_or_cik: str,
    metric: str,
    taxonomy: str = "us-gaap",
    tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch a company's reported financials from SEC EDGAR XBRL company-concept data.

    Free, no API key, but SEC policy requires a declarative User-Agent (sent
    automatically). ticker_or_cik accepts a ticker symbol (e.g. "AAPL") or a
    CIK number; metric is a human-readable name echoed in the summary while
    taxonomy + tag select the actual XBRL concept. Returns the most recent
    8 reported (end, val) observations. A 404 means the company or tag has no
    XBRL data — reported as an explicit error, never fabricated.
    """
    _check_tenant(headers)
    s = get_settings()
    identifier = ticker_or_cik.strip()
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        if identifier.isdigit():
            cik = identifier.zfill(10)
        else:
            map_resp = await client.get(SEC_TICKER_MAP_URL)
            if map_resp.status_code != 200:
                return json.dumps(
                    {"error": f"SEC ticker map {map_resp.status_code}: {map_resp.text[:200]}"}
                )
            ticker_upper = identifier.upper()
            cik = None
            for entry in map_resp.json().values():
                if str(entry.get("ticker", "")).upper() == ticker_upper:
                    cik = str(entry["cik_str"]).zfill(10)
                    break
            if cik is None:
                return json.dumps(
                    {
                        "error": "ticker_not_found",
                        "detail": f"{identifier} is not in SEC company_tickers.json",
                    }
                )

        concept_resp = await client.get(
            f"{s.sec_edgar_api_base.rstrip('/')}/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
        )
        if concept_resp.status_code != 200:
            return json.dumps(
                {"error": f"SEC EDGAR API {concept_resp.status_code}: {concept_resp.text[:200]}"}
            )
        data = concept_resp.json()

    units = data.get("units") or {}
    unit = next(iter(units), None)
    observations = [
        {"end": o.get("end"), "val": o.get("val")}
        for o in sorted(units.get(unit, []), key=lambda o: str(o.get("end") or ""), reverse=True)
    ][:8]
    return json.dumps(
        {
            "metric": metric,
            "taxonomy": taxonomy,
            "tag": tag,
            "cik": data.get("cik"),
            "entity_name": data.get("entityName"),
            "label": data.get("label"),
            "unit": unit,
            "observations": observations,
            "count": len(observations),
        }
    )


@mcp.tool(annotations=READ_ONLY)
@readonly
async def wikipedia_pageviews(
    article: str,
    start: str,
    end: str,
    granularity: str = "daily",
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch Wikipedia article pageview counts via the Wikimedia REST API.

    Free, no API key. start/end are YYYYMMDD dates (inclusive);
    granularity is daily or monthly. A 404 means the article has no
    pageview data — reported as an explicit error, never fabricated.
    """
    _check_tenant(headers)
    s = get_settings()
    url = (
        f"{s.wikimedia_rest_api_base.rstrip('/')}/metrics/pageviews/per-article"
        f"/en.wikipedia/all-access/user/{quote(article)}/{granularity}/{start}00/{end}00"
    )
    async with httpx.AsyncClient(
        timeout=s.external_http_timeout,
        headers={"User-Agent": s.external_user_agent},
        trust_env=False,
    ) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return json.dumps({"error": f"Wikimedia API {resp.status_code}: {resp.text[:200]}"})
        data = resp.json()

    points = [
        {"date": item.get("timestamp"), "views": int(item.get("views", 0))}
        for item in data.get("items", [])
    ]
    return json.dumps(
        {
            "article": article,
            "granularity": granularity,
            "points": points,
            "total_views": sum(p["views"] for p in points),
            "count": len(points),
        }
    )
