"""Unit tests for the research MCP server's provider chain.

Covers the free DuckDuckGo + Wikipedia path, the optional Tavily fallback,
the deterministic mock fallback, and every tenant-gated tool wrapper.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from adforge.mcp_servers.research import (
    _duckduckgo_search,
    _tavily_search,
    _wikipedia_search,
    competitor_creatives,
    keyword_clusters,
    serp_scrape,
    web_search,
    wikipedia_search,
)

HEADERS = {"X-AdForge-Tenant": "acme"}
DDG_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_REGEX = r"https://r\.jina\.ai/https://lite\.duckduckgo\.com/lite/.*"
BING_RSS_URL = "https://www.bing.com/search"
WIKI_URL = "https://en.wikipedia.org/w/api.php"
TAVILY_URL = "https://api.tavily.com/search"

DDG_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1">
  <b>Example</b> Result
</a>
<a class="result__snippet" href="x">A <b>useful</b> snippet.</a>
<a class="result__a" href="https://direct.example/b">Direct Result</a>
</body></html>
"""


def _wiki_opensearch(titles: list[str] | None = None) -> list:
    titles = titles or ["Project management", "Agile software development"]
    return [
        "query",
        titles,
        [f"desc {t}" for t in titles],
        [f"https://en.wikipedia.org/wiki/{t.replace(' ', '_')}" for t in titles],
    ]


class TestTenantGate:
    async def test_missing_tenant_is_rejected(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await web_search("anything")

    async def test_tools_share_the_gate(self):
        with pytest.raises(ValueError):
            await wikipedia_search("x")
        with pytest.raises(ValueError):
            await serp_scrape("x")
        with pytest.raises(ValueError):
            await keyword_clusters("x")
        with pytest.raises(ValueError):
            await competitor_creatives("example.com")


class TestDuckDuckGo:
    @respx.mock
    async def test_parses_redirect_and_direct_links(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text=DDG_HTML)
        )
        results = await _duckduckgo_search("project management", max_results=2)
        assert len(results) == 2
        assert results[0]["url"] == "https://example.com/a"
        assert results[0]["title"] == "Example Result"
        assert results[0]["snippet"] == "A useful snippet."
        assert results[1]["url"] == "https://direct.example/b"
        assert results[1]["snippet"] == ""

    @respx.mock
    async def test_strips_html_and_truncates_snippets(self):
        snippet = "<b>" + ("x" * 400) + "</b>"
        html = (
            '<a class="result__a" href="https://example.com">Title</a>'
            f'<a class="result__snippet">{snippet}</a>'
        )
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text=html)
        )
        results = await _duckduckgo_search("q", max_results=5)
        assert results[0]["snippet"] == "x" * 300

    @respx.mock
    async def test_http_error_returns_empty(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(429, text="slow down")
        )
        assert await _duckduckgo_search("q", 5) == []


class TestWikipediaProvider:
    @respx.mock
    async def test_opensearch_and_extracts(self):
        route = respx.get(WIKI_URL).mock(
            side_effect=[
                httpx.Response(200, json=_wiki_opensearch()),
                httpx.Response(
                    200,
                    json={
                        "query": {
                            "pages": {
                                "1": {"extract": "A long extract about project management."}
                            }
                        }
                    },
                ),
                httpx.Response(200, json={"query": {"pages": {"2": {}}}}),
            ]
        )
        results = await _wikipedia_search("project management", 2)
        assert [r["title"] for r in results] == [
            "Project management",
            "Agile software development",
        ]
        assert results[0]["snippet"].startswith("A long extract")
        assert results[1]["snippet"] == ""
        assert route.call_count == 3

    @respx.mock
    async def test_short_opensearch_response_returns_empty(self):
        respx.get(WIKI_URL).mock(return_value=httpx.Response(200, json=["q", []]))
        assert await _wikipedia_search("q", 3) == []

    @respx.mock
    async def test_opensearch_http_error(self):
        respx.get(WIKI_URL).mock(return_value=httpx.Response(500, text="down"))
        assert await _wikipedia_search("q", 3) == []

    @respx.mock
    async def test_extract_http_error_keeps_result_with_empty_snippet(self):
        respx.get(WIKI_URL).mock(
            side_effect=[
                httpx.Response(200, json=_wiki_opensearch(["Only"])),
                httpx.Response(503, text="down"),
            ]
        )
        results = await _wikipedia_search("only", 1)
        assert results == [
            {
                "title": "Only",
                "url": "https://en.wikipedia.org/wiki/Only",
                "snippet": "",
            }
        ]


class TestTavilyProvider:
    async def test_no_key_returns_error(self):
        assert await _tavily_search("q", 5) == {"error": "no_tavily_key"}

    @respx.mock
    async def test_success_maps_results(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        from adforge.config import get_settings

        get_settings.cache_clear()
        route = respx.post(TAVILY_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "answer": "Short answer",
                    "results": [
                        {
                            "title": "T1",
                            "url": "https://t1.example",
                            "content": "c" * 500,
                        }
                    ],
                },
            )
        )
        result = await _tavily_search("q", 3)
        assert result["answer"] == "Short answer"
        assert result["results"][0]["snippet"] == "c" * 300
        body = json.loads(route.calls[0].request.content)
        assert body == {
            "api_key": "tvly-test",
            "query": "q",
            "max_results": 3,
            "include_answer": True,
        }

    @respx.mock
    async def test_http_error_returns_error(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        from adforge.config import get_settings

        get_settings.cache_clear()
        respx.post(TAVILY_URL).mock(return_value=httpx.Response(401, text="no"))
        assert await _tavily_search("q", 3) == {"error": "tavily_401"}


class TestWebSearchChain:
    @respx.mock
    async def test_duckduckgo_wins(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text=DDG_HTML)
        )
        payload = json.loads(await web_search("q", max_results=1, headers=HEADERS))
        assert payload["provider"] == "duckduckgo"
        assert payload["results"][0]["title"] == "Example Result"

    @respx.mock
    async def test_wikipedia_fallback(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text="<html>no results</html>")
        )
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked")
        )
        respx.get(BING_RSS_URL).mock(return_value=httpx.Response(503, text="down"))
        respx.get(WIKI_URL).mock(
            side_effect=[
                httpx.Response(200, json=_wiki_opensearch(["Wiki One"])),
                httpx.Response(200, json={"query": {"pages": {"1": {"extract": "hi"}}}}),
            ]
        )
        payload = json.loads(await web_search("q", max_results=1, headers=HEADERS))
        assert payload["provider"] == "wikipedia"
        assert payload["results"][0]["title"] == "Wiki One"

    @respx.mock
    async def test_tavily_fallback(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        from adforge.config import get_settings

        get_settings.cache_clear()
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked")
        )
        respx.get(BING_RSS_URL).mock(return_value=httpx.Response(503, text="down"))
        respx.get(WIKI_URL).mock(return_value=httpx.Response(200, json=["q", []]))
        respx.post(TAVILY_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "answer": "answer",
                    "results": [{"title": "T", "url": "https://t", "content": "body"}],
                },
            )
        )
        payload = json.loads(await web_search("q", max_results=1, headers=HEADERS))
        assert payload == {
            "provider": "tavily",
            "query": "q",
            "results": [{"title": "T", "url": "https://t", "snippet": "body"}],
            "answer": "answer",
        }

    @respx.mock
    async def test_all_providers_fail_returns_explicit_empty(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(503, text="down")
        )
        respx.get(url__regex=DDG_LITE_REGEX).mock(
            return_value=httpx.Response(451, text="blocked")
        )
        respx.get(BING_RSS_URL).mock(return_value=httpx.Response(503, text="down"))
        respx.get(WIKI_URL).mock(return_value=httpx.Response(503, text="down"))
        payload = json.loads(await web_search("q", max_results=2, headers=HEADERS))
        assert payload["provider"] == "none"
        assert payload["results"] == []
        assert "no simulated results" in payload["error"]


class TestTools:
    @respx.mock
    async def test_wikipedia_tool_returns_count(self):
        respx.get(WIKI_URL).mock(
            side_effect=[
                httpx.Response(200, json=_wiki_opensearch(["One"])),
                httpx.Response(200, json={"query": {"pages": {"1": {"extract": "hi"}}}}),
            ]
        )
        payload = json.loads(
            await wikipedia_search("one", n=1, headers=HEADERS)
        )
        assert payload["query"] == "one"
        assert payload["count"] == 1

    @respx.mock
    async def test_serp_scrape_delegates_to_web_search(self):
        respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(
            return_value=httpx.Response(200, text=DDG_HTML)
        )
        payload = json.loads(await serp_scrape("q", top_n=1, headers=HEADERS))
        assert payload["provider"] == "duckduckgo"
        assert len(payload["results"]) == 1

    @respx.mock
    async def test_keyword_clusters_respects_cap(self):
        respx.get(url__regex=r"https://suggestqueries\.google\.com/.*").mock(
            return_value=httpx.Response(
                200, json=["crm", [f"suggestion {i}" for i in range(120)]]
            )
        )
        payload = json.loads(
            await keyword_clusters("crm", n=100, headers=HEADERS)
        )
        assert payload["seed"] == "crm"
        assert payload["source"] == "google_suggest"
        assert len(payload["clusters"]) == 100

    @respx.mock
    async def test_keyword_clusters_small_n(self):
        respx.get(url__regex=r"https://suggestqueries\.google\.com/.*").mock(
            return_value=httpx.Response(
                200, json=["crm", ["buy crm", "best crm", "cheap crm", "crm demo"]]
            )
        )
        payload = json.loads(await keyword_clusters("crm", n=3, headers=HEADERS))
        assert payload["clusters"] == ["buy crm", "best crm", "cheap crm"]

    @respx.mock
    async def test_keyword_clusters_explicit_error_when_suggest_unavailable(self):
        respx.get(url__regex=r"https://suggestqueries\.google\.com/.*").mock(
            return_value=httpx.Response(503, text="down")
        )
        payload = json.loads(await keyword_clusters("crm", n=3, headers=HEADERS))
        assert payload["clusters"] == []
        assert "no simulated keywords" in payload["error"]

    async def test_competitor_creatives_requires_token(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.delenv("META_AD_LIBRARY_TOKEN", raising=False)
        get_settings.cache_clear()
        payload = json.loads(
            await competitor_creatives("acme.example", headers=HEADERS)
        )
        assert payload["domain"] == "acme.example"
        assert payload["creatives"] == []
        assert payload["error"] == "meta_ad_library_token_not_configured"

    @respx.mock
    async def test_competitor_creatives_maps_meta_ads(self, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "library-token")
        get_settings.cache_clear()
        route = respx.get(url__regex=r"https://graph\.facebook\.com/.*/ads_archive").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "ad-1",
                            "ad_creative_link_titles": ["Try Acme today"],
                            "ad_creative_bodies": ["Body copy"],
                            "ad_creative_link_captions": ["acme.example"],
                        }
                    ]
                },
            )
        )
        payload = json.loads(
            await competitor_creatives("acme.example", headers=HEADERS)
        )
        assert payload["count"] == 1
        assert payload["creatives"][0]["headline"] == "Try Acme today"
        assert payload["creatives"][0]["platform"] == "meta"
        assert route.calls[0].request.url.params["search_terms"] == "acme.example"
