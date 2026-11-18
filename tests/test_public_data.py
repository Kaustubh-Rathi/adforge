"""Unit tests for the public-data MCP server.

Every external HTTP call is intercepted with ``respx`` so the suite never
touches the network. Tools are exercised directly as async functions, which is
how FastMCP invokes them in-process.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from adforge.config import get_settings
from adforge.mcp_servers import public_data

HEADERS = {"X-AdForge-Tenant": "acme"}


def _refresh_settings(monkeypatch, **env: str) -> None:
    """Set env vars and rebuild the cached ``Settings`` instance."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _json(result: str) -> dict:
    return json.loads(result)


class TestTenantGuard:
    async def test_missing_header_raises(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await public_data.similarweb_traffic("example.com", headers=None)

    async def test_header_without_tenant_key_raises(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await public_data.appstore_search("widget", headers={"X-Other": "1"})


class TestSimilarWebTraffic:
    async def test_missing_key_returns_explicit_config_error(self, monkeypatch):
        _refresh_settings(monkeypatch, SIMILARWEB_API_KEY="")

        data = _json(await public_data.similarweb_traffic("acme.com", headers=HEADERS))

        assert data["domain"] == "acme.com"
        assert data["error"] == "similarweb_not_configured"
        assert "SIMILARWEB_API_KEY" in data["detail"]
        assert "visits_30d" not in data

    @respx.mock
    async def test_key_returns_raw_payload(self, monkeypatch):
        _refresh_settings(monkeypatch, SIMILARWEB_API_KEY="sw-key")
        route = respx.get(
            url__regex=r"https://api\.similarweb\.com/v1/website/acme\.com/.*"
        ).mock(
            return_value=httpx.Response(
                200, json={"visits": [{"date": "2026-10", "value": 9000}]}
            )
        )

        data = _json(await public_data.similarweb_traffic("acme.com", headers=HEADERS))

        assert data == {
            "domain": "acme.com",
            "raw": {"visits": [{"date": "2026-10", "value": 9000}]},
        }
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer sw-key"
        assert request.url.params["granularity"] == "monthly"
        assert request.url.params["main_domain_only"] == "true"

    @respx.mock
    async def test_non_200_returns_error(self, monkeypatch):
        _refresh_settings(monkeypatch, SIMILARWEB_API_KEY="sw-key")
        respx.get(url__regex=r"https://api\.similarweb\.com/.*").mock(
            return_value=httpx.Response(429, text="rate limit exceeded")
        )

        data = _json(await public_data.similarweb_traffic("acme.com", headers=HEADERS))

        assert "SimilarWeb API 429" in data["error"]
        assert "rate limit exceeded" in data["error"]


class TestAppStoreSearch:
    @respx.mock
    async def test_success_maps_results(self):
        route = respx.get("https://itunes.apple.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 123,
                            "trackName": "Widget",
                            "sellerName": "Acme Inc",
                            "primaryGenreName": "Productivity",
                            "averageUserRating": 4.6,
                            "userRatingCount": 99,
                            "price": 0.0,
                            "trackViewUrl": "https://apps.apple.com/app/id123",
                        },
                        {"trackName": "Paid Widget"},
                    ]
                },
            )
        )

        data = _json(
            await public_data.appstore_search("widget", country="gb", headers=HEADERS)
        )

        assert data["query"] == "widget"
        assert data["country"] == "gb"
        assert data["apps"][0] == {
            "track_id": 123,
            "track_name": "Widget",
            "seller_name": "Acme Inc",
            "primary_genre": "Productivity",
            "user_rating": 4.6,
            "user_rating_count": 99,
            "price": 0.0,
            "url": "https://apps.apple.com/app/id123",
        }
        # Missing fields fall back to None / zero rather than raising.
        assert data["apps"][1]["track_id"] is None
        assert data["apps"][1]["price"] == 0

        request = route.calls[0].request
        assert request.url.params["term"] == "widget"
        assert request.url.params["country"] == "gb"
        assert request.url.params["media"] == "software"
        assert request.url.params["limit"] == "10"

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get("https://itunes.apple.com/search").mock(
            return_value=httpx.Response(503)
        )

        data = _json(await public_data.appstore_search("widget", headers=HEADERS))

        assert data == {"error": "iTunes API 503"}


class TestAppStoreReviews:
    @respx.mock
    async def test_success_skips_metadata_entry_and_maps_reviews(self):
        long_content = "great " * 120  # 720 chars -> truncated to 500
        route = respx.get(
            url__regex=(
                r"https://itunes\.apple\.com/gb/rss/customerreviews/"
                r"id=123/sort=mostrecent/json"
            )
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "feed": {
                        "entry": [
                            # First entry is app metadata, not a review.
                            {"im:name": {"label": "Widget"}},
                            {
                                "im:rating": {"label": "5"},
                                "title": {"label": "Great app"},
                                "content": {"label": long_content},
                                "author": {"name": {"label": "alice"}},
                                "im:version": {"label": "2.1"},
                            },
                            {
                                "im:rating": {"label": "2"},
                                "title": {"label": "Meh"},
                                "content": {"label": "Crashes sometimes"},
                                "author": {"name": {"label": "bob"}},
                                "im:version": {"label": "2.0"},
                            },
                            {
                                "im:rating": {"label": "4"},
                                "title": {"label": "Third"},
                                "content": {"label": "Beyond the limit"},
                                "author": {"name": {"label": "carol"}},
                                "im:version": {"label": "1.9"},
                            },
                        ]
                    }
                },
            )
        )

        data = _json(
            await public_data.appstore_reviews(
                app_id=123, country="gb", limit=2, headers=HEADERS
            )
        )

        assert data["app_id"] == 123
        assert data["country"] == "gb"
        assert data["count"] == 2
        assert [r["title"] for r in data["reviews"]] == ["Great app", "Meh"]
        first = data["reviews"][0]
        assert first["rating"] == 5
        assert len(first["content"]) == 500
        assert first["author"] == "alice"
        assert first["version"] == "2.1"
        assert data["reviews"][1]["rating"] == 2
        assert route.calls[0].request.url.path == (
            "/gb/rss/customerreviews/id=123/sort=mostrecent/json"
        )

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get(url__regex=r"https://itunes\.apple\.com/.*/rss/customerreviews/.*").mock(
            return_value=httpx.Response(404)
        )

        data = _json(
            await public_data.appstore_reviews(app_id=999, headers=HEADERS)
        )

        assert data == {"error": "iTunes RSS 404"}


class TestIpinfoLookup:
    @respx.mock
    async def test_success_without_token_omits_token_param(self, monkeypatch):
        _refresh_settings(monkeypatch, IPINFO_API_TOKEN="")
        route = respx.get(url__regex=r"https://ipinfo\.io/8\.8\.8\.8/json.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ip": "8.8.8.8",
                    "city": "Mountain View",
                    "region": "California",
                    "country": "US",
                    "org": "AS15169 Google LLC",
                    "hostname": "dns.google",
                    "loc": "37.4056,-122.0775",
                },
            )
        )

        data = _json(await public_data.ipinfo_lookup("8.8.8.8", headers=HEADERS))

        assert data == {
            "ip": "8.8.8.8",
            "city": "Mountain View",
            "region": "California",
            "country": "US",
            "org": "AS15169 Google LLC",
            "hostname": "dns.google",
            "loc": "37.4056,-122.0775",
        }
        assert "token" not in route.calls[0].request.url.params

    @respx.mock
    async def test_success_with_token_sends_token_param(self, monkeypatch):
        _refresh_settings(monkeypatch, IPINFO_API_TOKEN="ip-token")
        route = respx.get(url__regex=r"https://ipinfo\.io/1\.1\.1\.1/json.*").mock(
            return_value=httpx.Response(
                200, json={"ip": "1.1.1.1", "city": "Sydney", "country": "AU"}
            )
        )

        data = _json(await public_data.ipinfo_lookup("1.1.1.1", headers=HEADERS))

        assert data["ip"] == "1.1.1.1"
        assert data["city"] == "Sydney"
        assert data["country"] == "AU"
        # Fields absent from the payload are still present as None.
        assert data["org"] is None
        assert data["loc"] is None
        assert route.calls[0].request.url.params["token"] == "ip-token"

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get(url__regex=r"https://ipinfo\.io/.*").mock(
            return_value=httpx.Response(429)
        )

        data = _json(await public_data.ipinfo_lookup("8.8.8.8", headers=HEADERS))

        assert data == {"error": "ipinfo API 429"}


class TestAbuseIpdbCheck:
    async def test_missing_key_returns_explicit_config_error(self, monkeypatch):
        _refresh_settings(monkeypatch, ABUSEIPDB_API_KEY="")

        data = _json(await public_data.abuseipdb_check("10.0.0.1", headers=HEADERS))

        assert data["ip"] == "10.0.0.1"
        assert data["error"] == "abuseipdb_not_configured"
        assert "ABUSEIPDB_API_KEY" in data["detail"]
        assert "abuse_confidence_score" not in data

    @respx.mock
    async def test_success_with_key_maps_response(self, monkeypatch):
        _refresh_settings(monkeypatch, ABUSEIPDB_API_KEY="abuse-key")
        route = respx.get("https://api.abuseipdb.com/api/v2/check").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "abuseConfidenceScore": 87,
                        "totalReports": 12,
                        "countryCode": "RU",
                        "usageType": "Data Center/Web Hosting/Transit",
                        "isp": "Evil Hosting",
                        "domain": "evil.example",
                    }
                },
            )
        )

        data = _json(await public_data.abuseipdb_check("203.0.113.7", headers=HEADERS))

        assert data == {
            "ip": "203.0.113.7",
            "abuse_confidence_score": 87,
            "total_reports": 12,
            "country": "RU",
            "usage_type": "Data Center/Web Hosting/Transit",
            "isp": "Evil Hosting",
            "domain": "evil.example",
        }
        request = route.calls[0].request
        assert request.headers["Key"] == "abuse-key"
        assert request.headers["Accept"] == "application/json"
        assert request.url.params["ipAddress"] == "203.0.113.7"
        assert request.url.params["maxAgeInDays"] == "90"

    @respx.mock
    async def test_non_200_returns_error(self, monkeypatch):
        _refresh_settings(monkeypatch, ABUSEIPDB_API_KEY="abuse-key")
        respx.get("https://api.abuseipdb.com/api/v2/check").mock(
            return_value=httpx.Response(403)
        )

        data = _json(await public_data.abuseipdb_check("203.0.113.7", headers=HEADERS))

        assert data == {"error": "AbuseIPDB 403"}


class TestGithubRepoStats:
    @respx.mock
    async def test_success_without_token_has_no_authorization_header(self, monkeypatch):
        _refresh_settings(monkeypatch, GITHUB_TOKEN="")
        route = respx.get("https://api.github.com/repos/acme/widget").mock(
            return_value=httpx.Response(
                200,
                json={
                    "stargazers_count": 1500,
                    "forks_count": 120,
                    "open_issues_count": 34,
                    "language": "Python",
                    "license": {"name": "MIT License"},
                    "pushed_at": "2026-10-01T12:00:00Z",
                    "description": "A widget library",
                },
            )
        )

        data = _json(await public_data.github_repo_stats("acme/widget", headers=HEADERS))

        assert data == {
            "repo": "acme/widget",
            "stars": 1500,
            "forks": 120,
            "open_issues": 34,
            "language": "Python",
            "license": "MIT License",
            "last_push": "2026-10-01T12:00:00Z",
            "description": "A widget library",
        }
        request = route.calls[0].request
        assert "Authorization" not in request.headers
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"
        assert request.headers["Accept"] == "application/vnd.github+json"

    @respx.mock
    async def test_success_with_token_sends_bearer_header(self, monkeypatch):
        _refresh_settings(monkeypatch, GITHUB_TOKEN="gh-token")
        route = respx.get("https://api.github.com/repos/acme/widget").mock(
            return_value=httpx.Response(
                200,
                json={
                    "stargazers_count": 1,
                    "forks_count": 0,
                    "open_issues_count": 0,
                    "language": None,
                    "license": None,
                    "pushed_at": None,
                    "description": None,
                },
            )
        )

        data = _json(await public_data.github_repo_stats("acme/widget", headers=HEADERS))

        assert data["license"] is None
        assert data["language"] is None
        assert route.calls[0].request.headers["Authorization"] == "Bearer gh-token"

    @respx.mock
    async def test_non_200_returns_error_with_body_snippet(self, monkeypatch):
        _refresh_settings(monkeypatch, GITHUB_TOKEN="")
        respx.get("https://api.github.com/repos/acme/missing").mock(
            return_value=httpx.Response(404, text='{"message": "Not Found"}')
        )

        data = _json(await public_data.github_repo_stats("acme/missing", headers=HEADERS))

        assert "GitHub API 404" in data["error"]
        assert "Not Found" in data["error"]


class TestHackerNewsTop:
    async def test_invalid_story_type_returns_error(self):
        data = _json(
            await public_data.hackernews_top(story_type="random", headers=HEADERS)
        )

        assert data == {
            "error": "story_type must be one of: top, new, best, ask, show, job"
        }

    @respx.mock
    async def test_success_skips_failed_and_empty_items(self):
        ids_route = respx.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json"
        ).mock(return_value=httpx.Response(200, json=[101, 102, 103, 104, 105]))
        respx.get("https://hacker-news.firebaseio.com/v0/item/101.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 101,
                    "title": "Show HN: Widget",
                    "url": "https://widget.example",
                    "score": 250,
                    "descendants": 42,
                    "by": "alice",
                    "time": 1_700_000_000,
                },
            )
        )
        respx.get("https://hacker-news.firebaseio.com/v0/item/102.json").mock(
            return_value=httpx.Response(500, text="boom")
        )
        respx.get("https://hacker-news.firebaseio.com/v0/item/103.json").mock(
            return_value=httpx.Response(
                200, json={"id": 103, "title": "Ask HN: Anything?", "score": 3}
            )
        )
        respx.get("https://hacker-news.firebaseio.com/v0/item/104.json").mock(
            return_value=httpx.Response(200, json={})
        )
        item_105_route = respx.get(
            "https://hacker-news.firebaseio.com/v0/item/105.json"
        ).mock(return_value=httpx.Response(200, json={"id": 105}))

        data = _json(
            await public_data.hackernews_top(story_type="top", n=4, headers=HEADERS)
        )

        assert ids_route.call_count == 1
        assert item_105_route.call_count == 0  # ids sliced to n before fetching
        assert data["story_type"] == "top"
        assert data["count"] == 2
        assert [s["id"] for s in data["stories"]] == [101, 103]

        first, fallback = data["stories"]
        assert first == {
            "id": 101,
            "title": "Show HN: Widget",
            "url": "https://widget.example",
            "score": 250,
            "comments": 42,
            "by": "alice",
            "time": 1_700_000_000,
        }
        # No explicit url -> canonical HN item URL; absent counters default to 0.
        assert fallback["url"] == "https://news.ycombinator.com/item?id=103"
        assert fallback["score"] == 3
        assert fallback["comments"] == 0

    @respx.mock
    async def test_ids_request_non_200_returns_error(self):
        respx.get("https://hacker-news.firebaseio.com/v0/newstories.json").mock(
            return_value=httpx.Response(503)
        )

        data = _json(
            await public_data.hackernews_top(story_type="new", n=2, headers=HEADERS)
        )

        assert data == {"error": "HN API 503"}


class TestRedditSearch:
    async def test_invalid_sort_returns_error(self):
        data = _json(
            await public_data.reddit_search("widget", sort="rising", headers=HEADERS)
        )

        assert data == {
            "error": "sort must be one of: relevance, hot, top, new, comments"
        }

    @respx.mock
    async def test_success_maps_posts(self):
        long_selftext = "body " * 150  # 750 chars -> truncated to 500
        route = respx.get("https://www.reddit.com/r/SaaS/search.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "Best CRM?",
                                    "url": "https://example.com/crm",
                                    "permalink": "/r/SaaS/comments/1/best_crm/",
                                    "subreddit": "SaaS",
                                    "score": 42,
                                    "num_comments": 8,
                                    "upvote_ratio": 0.91,
                                    "author": "alice",
                                    "created_utc": 1_700_000_000.0,
                                    "selftext": long_selftext,
                                }
                            },
                            {"data": {"title": "Link post"}},
                        ]
                    }
                },
            )
        )

        data = _json(
            await public_data.reddit_search(
                "crm", subreddit="SaaS", n=5, sort="top", headers=HEADERS
            )
        )

        assert data["query"] == "crm"
        assert data["subreddit"] == "SaaS"
        assert data["sort"] == "top"
        assert data["count"] == 2
        first, second = data["posts"]
        assert first["title"] == "Best CRM?"
        assert first["permalink"] == "https://reddit.com/r/SaaS/comments/1/best_crm/"
        assert first["score"] == 42
        assert first["comments"] == 8
        assert first["upvote_ratio"] == 0.91
        assert first["author"] == "alice"
        assert first["created_utc"] == 1_700_000_000.0
        assert len(first["selftext"]) == 500
        # Missing fields use safe defaults.
        assert second["score"] == 0
        assert second["comments"] == 0
        assert second["selftext"] == ""
        assert second["permalink"] == "https://reddit.com"

        request = route.calls[0].request
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"
        assert request.url.params["q"] == "crm"
        assert request.url.params["sort"] == "top"
        assert request.url.params["limit"] == "5"
        assert request.url.params["restrict_sr"] == "true"
        assert request.url.params["t"] == "year"

    @respx.mock
    async def test_non_200_returns_error_with_body_snippet(self):
        respx.get(url__regex=r"https://www\.reddit\.com/r/.*/search\.json.*").mock(
            return_value=httpx.Response(403, text="blocked")
        )

        data = _json(await public_data.reddit_search("crm", headers=HEADERS))

        assert "Reddit API 403" in data["error"]
        assert "blocked" in data["error"]


class TestStackExchangeSearch:
    @respx.mock
    async def test_success_maps_items_and_respects_n(self):
        route = respx.get("https://api.stackexchange.com/2.3/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "title": "How do I use Widget?",
                            "link": "https://stackoverflow.com/q/1",
                            "score": 12,
                            "answer_count": 3,
                            "is_answered": True,
                            "tags": ["python", "widget"],
                            "view_count": 5000,
                            "creation_date": 1_700_000_000,
                        },
                        {"title": "Widget is slow"},
                        {"title": "Third result"},
                    ]
                },
            )
        )

        data = _json(
            await public_data.stackexchange_search(
                "widget", site="serverfault", n=2, headers=HEADERS
            )
        )

        assert data["query"] == "widget"
        assert data["site"] == "serverfault"
        assert data["count"] == 2
        assert data["items"][0] == {
            "title": "How do I use Widget?",
            "link": "https://stackoverflow.com/q/1",
            "score": 12,
            "answers": 3,
            "is_answered": True,
            "tags": ["python", "widget"],
            "view_count": 5000,
            "creation_date": 1_700_000_000,
        }
        assert data["items"][1]["score"] == 0
        assert data["items"][1]["answers"] == 0
        assert data["items"][1]["tags"] == []

        request = route.calls[0].request
        assert request.url.params["order"] == "desc"
        assert request.url.params["sort"] == "relevance"
        assert request.url.params["intitle"] == "widget"
        assert request.url.params["site"] == "serverfault"
        assert request.url.params["pagesize"] == "2"

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get("https://api.stackexchange.com/2.3/search").mock(
            return_value=httpx.Response(502)
        )

        data = _json(
            await public_data.stackexchange_search("widget", headers=HEADERS)
        )

        assert data == {"error": "StackExchange API 502"}


class TestProductHuntSearch:
    async def test_missing_token_returns_explicit_config_error(self, monkeypatch):
        _refresh_settings(monkeypatch, PRODUCTHUNT_API_TOKEN="")

        data = _json(await public_data.producthunt_search("widget", headers=HEADERS))

        assert data["error"] == "producthunt_not_configured"
        assert "PRODUCTHUNT_API_TOKEN" in data["detail"]

    @respx.mock
    async def test_success_maps_graphql_nodes(self, monkeypatch):
        _refresh_settings(monkeypatch, PRODUCTHUNT_API_TOKEN="ph-token")
        route = respx.post("https://api.producthunt.com/v2/api/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "posts": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "1",
                                        "name": "Widget",
                                        "tagline": "Ship faster",
                                        "url": "https://producthunt.com/posts/widget",
                                        "votesCount": 120,
                                        "commentsCount": 33,
                                        "createdAt": "2026-10-01T10:00:00Z",
                                    }
                                },
                                {
                                    "node": {
                                        "id": "2",
                                        "name": "Gadget",
                                        "tagline": None,
                                        "url": None,
                                        "votesCount": 0,
                                        "commentsCount": 0,
                                        "createdAt": None,
                                    }
                                },
                            ]
                        }
                    }
                },
            )
        )

        data = _json(await public_data.producthunt_search("widget", headers=HEADERS))

        assert data["query"] == "widget"
        assert data["source"] == "producthunt"
        assert data["count"] == 2
        assert data["hits"][0] == {
            "id": "1",
            "title": "Widget",
            "tagline": "Ship faster",
            "url": "https://producthunt.com/posts/widget",
            "points": 120,
            "comments": 33,
            "created_at": "2026-10-01T10:00:00Z",
        }
        assert data["hits"][1]["points"] == 0
        assert data["hits"][1]["comments"] == 0

        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer ph-token"
        assert b'"query":"widget"' in request.content.replace(b" ", b"")

    @respx.mock
    async def test_non_200_returns_error(self, monkeypatch):
        _refresh_settings(monkeypatch, PRODUCTHUNT_API_TOKEN="ph-token")
        respx.post("https://api.producthunt.com/v2/api/graphql").mock(
            return_value=httpx.Response(500)
        )

        data = _json(await public_data.producthunt_search("widget", headers=HEADERS))

        assert data["error"].startswith("Product Hunt API 500")


class TestOpenCorporatesSearch:
    @respx.mock
    async def test_success_maps_companies_and_caps_per_page(self):
        route = respx.get(
            "https://api.opencorporates.com/v0.4/companies/search"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": {
                        "companies": [
                            {
                                "company": {
                                    "name": "Acme Widgets Ltd",
                                    "jurisdiction_code": "gb",
                                    "company_number": "01234567",
                                    "incorporation_date": "2010-04-12",
                                    "current_status": "Active",
                                    "registered_address_in_full": "1 Main St, London",
                                    "opencorporates_url": "https://opencorporates.com/companies/gb/01234567",
                                }
                            },
                            {
                                "company": {
                                    "name": "Acme Widgets Inc",
                                    "jurisdiction_code": "us_de",
                                    "company_number": "7654321",
                                }
                            },
                        ]
                    }
                },
            )
        )

        data = _json(
            await public_data.opencorporates_search("acme widgets", n=50, headers=HEADERS)
        )

        assert data["query"] == "acme widgets"
        assert data["count"] == 2
        assert data["companies"][0] == {
            "name": "Acme Widgets Ltd",
            "jurisdiction": "gb",
            "company_number": "01234567",
            "incorporation_date": "2010-04-12",
            "status": "Active",
            "address": "1 Main St, London",
            "opencorporates_url": "https://opencorporates.com/companies/gb/01234567",
        }
        assert data["companies"][1]["name"] == "Acme Widgets Inc"
        assert data["companies"][1]["status"] is None

        request = route.calls[0].request
        assert request.url.params["q"] == "acme widgets"
        # The API page size is capped at 30 regardless of n.
        assert request.url.params["per_page"] == "30"

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get("https://api.opencorporates.com/v0.4/companies/search").mock(
            return_value=httpx.Response(401)
        )

        data = _json(
            await public_data.opencorporates_search("acme widgets", headers=HEADERS)
        )

        assert data == {"error": "OpenCorporates API 401"}


class TestWikipediaSearchDirect:
    @respx.mock
    async def test_success_maps_titles_and_urls(self):
        route = respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(
                200,
                json=[
                    "acme",
                    ["Acme Corporation", "Acme (disambiguation)"],
                    ["American company", "Topics referred to by the same term"],
                    [
                        "https://en.wikipedia.org/wiki/Acme_Corporation",
                        "https://en.wikipedia.org/wiki/Acme_(disambiguation)",
                    ],
                ],
            )
        )

        data = _json(
            await public_data.wikipedia_search_direct(
                "acme", n=2, headers=HEADERS
            )
        )

        assert data == {
            "query": "acme",
            "results": [
                {
                    "title": "Acme Corporation",
                    "url": "https://en.wikipedia.org/wiki/Acme_Corporation",
                },
                {
                    "title": "Acme (disambiguation)",
                    "url": "https://en.wikipedia.org/wiki/Acme_(disambiguation)",
                },
            ],
            "count": 2,
        }
        request = route.calls[0].request
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"
        assert request.url.params["action"] == "opensearch"
        assert request.url.params["search"] == "acme"
        assert request.url.params["limit"] == "2"
        assert request.url.params["namespace"] == "0"
        assert request.url.params["format"] == "json"
        assert request.url.params["origin"] == "*"

    @respx.mock
    async def test_short_response_returns_empty_results(self):
        respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json=["acme", []])
        )

        data = _json(
            await public_data.wikipedia_search_direct("acme", headers=HEADERS)
        )

        assert data == {"query": "acme", "results": []}

    @respx.mock
    async def test_non_200_returns_error(self):
        respx.get("https://en.wikipedia.org/w/api.php").mock(
            return_value=httpx.Response(429)
        )

        data = _json(
            await public_data.wikipedia_search_direct("acme", headers=HEADERS)
        )

        assert data == {"error": "Wikipedia API 429"}
