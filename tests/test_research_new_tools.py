"""Unit tests for the research MCP server's keyless tools.

Covers trends_interest, fetch_page and the web_search provider chain's
DuckDuckGo-Lite-via-Jina-Reader and Bing RSS fallbacks. Every external
HTTP call is intercepted with ``respx``. Tools are exercised directly as
async functions, matching the style of tests/test_research_mcp.py.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from adforge.mcp_servers.research import (
    _bing_rss_search,
    _ddg_lite_search,
    fetch_page,
    trends_interest,
    web_search,
)

HEADERS = {"X-AdForge-Tenant": "acme"}

TRENDS_BASE = "https://trends.google.com"
WARMUP_URL = f"{TRENDS_BASE}/trends/"
EXPLORE_URL = f"{TRENDS_BASE}/trends/api/explore"
MULTILINE_URL = f"{TRENDS_BASE}/trends/api/widgetdata/multiline"
JINA_URL = "https://r.jina.ai"

EXPLORE_BODY = ")]}'\n" + json.dumps({
    "widgets": [
        {"id": "TIMESERIES", "token": "DR45abcdTOKEN", "request": {"time": "today 12-m"}},
        {"id": "RELATED_QUERIES", "token": "other-token"},
    ]
})

MULTILINE_BODY = ")]}'\n" + json.dumps({
    "default": {
        "timelineData": [
            {"formattedTime": "Nov 16 - 22, 2025", "value": [58]},
            {"formattedTime": "Nov 23 - 29, 2025", "value": [62]},
            {"formattedTime": "Nov 30 - Dec 6, 2025", "value": [70]},
        ]
    }
})


def _mock_trends_flow(explore_response=None, multiline_response=None):
    warm = respx.get(WARMUP_URL).mock(
        return_value=httpx.Response(200, text="<html>ok</html>",
                                  headers={"Set-Cookie": "NID=test-cookie"})
    )
    explore = respx.get(EXPLORE_URL).mock(
        return_value=explore_response or httpx.Response(200, text=EXPLORE_BODY)
    )
    multiline = respx.get(MULTILINE_URL).mock(
        return_value=multiline_response or httpx.Response(200, text=MULTILINE_BODY)
    )
    return warm, explore, multiline


class TestTenantGate:
    async def test_trends_requires_tenant_header(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await trends_interest("crm software")

    async def test_fetch_page_requires_tenant_header(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await fetch_page("https://example.com")


class TestTrendsInterest:
    @respx.mock
    async def test_happy_path_returns_series(self):
        _mock_trends_flow()
        payload = json.loads(
            await trends_interest("crm software", headers=HEADERS)
        )
        assert payload["keyword"] == "crm software"
        assert payload["geo"] == "US"
        assert payload["timeframe"] == "today 12-m"
        assert payload["source"] == "google_trends"
        assert payload["points"] == [
            {"date": "Nov 16 - 22, 2025", "value": 58},
            {"date": "Nov 23 - 29, 2025", "value": 62},
            {"date": "Nov 30 - Dec 6, 2025", "value": 70},
        ]
        assert payload["average"] == round((58 + 62 + 70) / 3, 1)

    @respx.mock
    async def test_request_shape_and_cookie_flow(self):
        warm, explore, multiline = _mock_trends_flow()
        await trends_interest(
            "vpn", geo="GB", timeframe="today 3-m", headers=HEADERS
        )
        assert warm.call_count == explore.call_count == multiline.call_count == 1
        # Warm-up cookie is carried into the widget requests.
        assert explore.calls[0].request.headers["Cookie"] == "NID=test-cookie"
        assert multiline.calls[0].request.headers["Cookie"] == "NID=test-cookie"

        explore_params = explore.calls[0].request.url.params
        assert explore_params["hl"] == "en-US"
        assert explore_params["tz"] == "0"
        explore_req = json.loads(explore_params["req"])
        assert explore_req == {
            "comparisonItem": [{"keyword": "vpn", "geo": "GB", "time": "today 3-m"}],
            "category": 0,
            "property": "",
        }

        ml_params = multiline.calls[0].request.url.params
        assert ml_params["token"] == "DR45abcdTOKEN"
        assert ml_params["hl"] == "en-US"
        ml_req = json.loads(ml_params["req"])
        assert ml_req["time"] == "today 3-m"
        assert ml_req["resolution"] == "WEEK"
        assert ml_req["comparisonItem"] == [
            {"keyword": "vpn", "geo": "GB", "time": "today 3-m"}
        ]
        assert ml_req["requestOptions"] == {
            "property": "", "backend": "IZG", "category": 0
        }

    @respx.mock
    async def test_explore_http_500_returns_explicit_error(self):
        _mock_trends_flow(explore_response=httpx.Response(500, text="server error"))
        payload = json.loads(await trends_interest("crm", headers=HEADERS))
        assert payload["error"] == "google_trends_http_500"
        assert "server error" in payload["detail"]
        assert payload["keyword"] == "crm"

    @respx.mock
    async def test_missing_token_returns_explicit_error(self):
        _mock_trends_flow(
            explore_response=httpx.Response(200, text=")]}'\n" + json.dumps({"widgets": []}))
        )
        payload = json.loads(await trends_interest("crm", headers=HEADERS))
        assert payload["error"] == "google_trends_widget_unavailable"
        assert "points" not in payload

    @respx.mock
    async def test_multiline_http_error_returns_explicit_error(self):
        _mock_trends_flow(multiline_response=httpx.Response(429, text="too many"))
        payload = json.loads(await trends_interest("crm", headers=HEADERS))
        assert payload["error"] == "google_trends_http_429"

    @respx.mock
    async def test_unparsable_explore_body_returns_explicit_error(self):
        _mock_trends_flow(explore_response=httpx.Response(200, text=")]}'\nnot-json"))
        payload = json.loads(await trends_interest("crm", headers=HEADERS))
        assert payload["error"] == "google_trends_explore_unparsable"


class TestFetchPage:
    @respx.mock
    async def test_happy_path_returns_markdown_text(self):
        route = respx.get(f"{JINA_URL}/https://example.com/page").mock(
            return_value=httpx.Response(200, text="# Title\n\nSome markdown body.")
        )
        result = await fetch_page("https://example.com/page", headers=HEADERS)
        assert result == "# Title\n\nSome markdown body."
        request = route.calls[0].request
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"

    @respx.mock
    async def test_long_body_truncated_to_max_tool_result_chars(self):
        from adforge.config import get_settings

        limit = get_settings().agent_max_tool_result_chars
        respx.get(url__regex=r"https://r\.jina\.ai/.*").mock(
            return_value=httpx.Response(200, text="x" * (limit + 500))
        )
        result = await fetch_page("https://example.com/long", headers=HEADERS)
        assert len(result) == limit

    @respx.mock
    async def test_non_200_returns_explicit_error(self):
        respx.get(url__regex=r"https://r\.jina\.ai/.*").mock(
            return_value=httpx.Response(403, text="forbidden by reader")
        )
        payload = json.loads(await fetch_page("https://example.com", headers=HEADERS))
        assert payload["url"] == "https://example.com"
        assert payload["error"] == "jina_reader_http_403"
        assert "forbidden" in payload["detail"]


DDG_SEARCH_REGEX = r"https://html\.duckduckgo\.com/html/.*"
DDG_LITE_REGEX = r"https://r\.jina\.ai/https://lite\.duckduckgo\.com/lite/.*"
BING_RSS_URL = "https://www.bing.com/search"
WIKI_URL = "https://en.wikipedia.org/w/api.php"

# DuckDuckGo anomaly/challenge page: HTTP 200 but zero result__a links.
DDG_ANOMALY_HTML = """
<html><body>
<h1>Unfortunately, bots use DuckDuckGo too.</h1>
<p>Please complete the challenge to continue.</p>
</body></html>
"""

DDG_RESULTS_HTML = """
<html><body>
<a class="result__a" href="https://direct.example/b">Direct Result</a>
<a class="result__snippet" href="x">A real snippet.</a>
</body></html>
"""

# Shape of a Jina-Reader rendering of DuckDuckGo Lite's results page.
DDG_LITE_MARKDOWN = """Title: product analytics at DuckDuckGo

URL Source: https://lite.duckduckgo.com/lite/?q=product+analytics

Markdown Content:
1.[Product Analytics Market Size, Share, Analysis | Report 2035](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.expertmarketresearch.com%2Freports%2Fproduct%2Danalytics%2Dmarket&rut=deadbeef)
The global product analytics market size was valued at USD 15.6 Billion in 2035.
2.[**What Is Product Analytics?**](https://mixpanel.com/blog/product-analytics)
Product analytics is the collection and analysis of data about how users
interact with a product.
3.[Ads Help Center](https://help.example.com/ads)
Paid search management center.
"""

# Shape of Bing's RSS 2.0 search endpoint response (format=rss).
BING_RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
<title>product analytics - Bing</title>
<item>
<title>Product Analytics Tools Compared</title>
<link>https://amplitude.com/blog/product-analytics</link>
<description>Compare the best &lt;b&gt;product analytics&lt;/b&gt; platforms for tracking user behavior.</description>
</item>
<item>
<title>What Is Product Analytics?</title>
<link>https://mixpanel.com/blog/product-analytics</link>
<description><![CDATA[Product analytics is the collection and <em>analysis</em> of data.]]></description>
</item>
<item>
<title>Item without a link</title>
<link></link>
<description>Should be dropped.</description>
</item>
</channel>
</rss>
"""


class TestDdgLiteSearch:
    @respx.mock
    async def test_parses_numbered_links_and_snippets(self):
        route = respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_LITE_MARKDOWN)
        )
        results = await _ddg_lite_search("product analytics", 5)
        assert len(results) == 3
        # uddg redirect is unwrapped and URL-decoded.
        assert results[0] == {
            "title": "Product Analytics Market Size, Share, Analysis | Report 2035",
            "url": "https://www.expertmarketresearch.com/reports/product-analytics-market",
            "snippet": "The global product analytics market size was valued at USD 15.6 Billion in 2035.",
        }
        # Multi-line snippets are joined; markdown emphasis is stripped.
        assert results[1]["title"] == "What Is Product Analytics?"
        assert results[1]["url"] == "https://mixpanel.com/blog/product-analytics"
        assert results[1]["snippet"] == (
            "Product analytics is the collection and analysis of data about how users "
            "interact with a product."
        )
        assert route.call_count == 1
        request = route.calls[0].request
        assert request.url == (
            "https://r.jina.ai/https://lite.duckduckgo.com/lite/?q=product+analytics"
        )
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"

    @respx.mock
    async def test_respects_max_results(self):
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_LITE_MARKDOWN)
        )
        assert len(await _ddg_lite_search("product analytics", 2)) == 2

    @respx.mock
    async def test_non_200_returns_empty(self):
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked by reader")
        )
        assert await _ddg_lite_search("q", 5) == []


class TestBingRssSearch:
    @respx.mock
    async def test_parses_rss_items_and_strips_html(self):
        route = respx.get(BING_RSS_URL).mock(
            return_value=httpx.Response(200, text=BING_RSS_XML)
        )
        results = await _bing_rss_search("product analytics", 5)
        # The link-less item is dropped.
        assert len(results) == 2
        # Entity-encoded tags are unescaped then stripped.
        assert results[0] == {
            "title": "Product Analytics Tools Compared",
            "url": "https://amplitude.com/blog/product-analytics",
            "snippet": "Compare the best product analytics platforms for tracking user behavior.",
        }
        # Raw CDATA tags are stripped too.
        assert results[1]["snippet"] == "Product analytics is the collection and analysis of data."
        params = route.calls[0].request.url.params
        assert params["q"] == "product analytics"
        assert params["format"] == "rss"
        assert route.calls[0].request.headers["User-Agent"].startswith("Mozilla/5.0")

    @respx.mock
    async def test_malformed_xml_returns_empty_not_raise(self):
        respx.get(BING_RSS_URL).mock(
            return_value=httpx.Response(200, text="<rss><channel><item>")
        )
        assert await _bing_rss_search("q", 3) == []

    @respx.mock
    async def test_http_error_returns_empty(self):
        respx.get(BING_RSS_URL).mock(return_value=httpx.Response(429, text="slow down"))
        assert await _bing_rss_search("q", 3) == []


class TestWebSearchFallbackChain:
    @respx.mock
    async def test_ddg_anomaly_falls_back_to_ddg_lite(self):
        respx.get(url__regex=DDG_SEARCH_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_ANOMALY_HTML)
        )
        lite = respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_LITE_MARKDOWN)
        )
        payload = json.loads(
            await web_search("product analytics", max_results=5, headers=HEADERS)
        )
        assert payload["provider"] == "ddg_lite"
        assert payload["query"] == "product analytics"
        assert len(payload["results"]) == 3
        assert payload["results"][0]["url"] == (
            "https://www.expertmarketresearch.com/reports/product-analytics-market"
        )
        assert lite.call_count == 1

    @respx.mock
    async def test_ddg_lite_failure_falls_back_to_bing_rss(self):
        respx.get(url__regex=DDG_SEARCH_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_ANOMALY_HTML)
        )
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked by reader")
        )
        respx.get(BING_RSS_URL).mock(
            return_value=httpx.Response(200, text=BING_RSS_XML)
        )
        payload = json.loads(
            await web_search("product analytics", max_results=5, headers=HEADERS)
        )
        assert payload["provider"] == "bing_rss"
        assert payload["results"][0]["title"] == "Product Analytics Tools Compared"

    @respx.mock
    async def test_all_free_providers_fail_falls_through_to_explicit_empty(self):
        respx.get(url__regex=DDG_SEARCH_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_ANOMALY_HTML)
        )
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked by reader")
        )
        respx.get(BING_RSS_URL).mock(return_value=httpx.Response(503, text="down"))
        respx.get(WIKI_URL).mock(return_value=httpx.Response(503, text="down"))
        payload = json.loads(await web_search("q", max_results=2, headers=HEADERS))
        assert payload["provider"] == "none"
        assert payload["results"] == []
        assert "no simulated results" in payload["error"]

    @respx.mock
    async def test_ddg_success_skips_fallbacks(self):
        lite = respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_LITE_MARKDOWN)
        )
        bing = respx.get(BING_RSS_URL).mock(
            return_value=httpx.Response(200, text=BING_RSS_XML)
        )
        respx.get(url__regex=DDG_SEARCH_REGEX).mock(
            return_value=httpx.Response(200, text=DDG_RESULTS_HTML)
        )
        payload = json.loads(await web_search("q", max_results=2, headers=HEADERS))
        assert payload["provider"] == "duckduckgo"
        assert payload["results"][0]["url"] == "https://direct.example/b"
        assert lite.call_count == 0
        assert bing.call_count == 0
