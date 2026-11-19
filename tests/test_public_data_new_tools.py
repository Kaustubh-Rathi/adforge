"""Unit tests for the SEC EDGAR + Wikimedia pageviews tools on the public-data
MCP server. All HTTP is intercepted with ``respx``; nothing touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from adforge.config import get_settings
from adforge.mcp_servers import public_data

HEADERS = {"X-AdForge-Tenant": "acme"}

CONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/"
    "RevenueFromContractWithCustomerExcludingAssessedTax.json"
)
PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "en.wikipedia/all-access/user/Python%20%28programming%20language%29/daily/"
    "2026110100/2026110300"
)


def _json(result: str) -> dict:
    return json.loads(result)


def _concept_payload(n: int = 10) -> dict:
    # Unsorted deliberately — the tool must sort by `end` descending.
    rows = [
        {"start": f"2025-{i:02d}-01", "end": f"2025-{i:02d}-28", "val": 1000 + i}
        for i in range(1, n + 1)
    ]
    rows.reverse()
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "label": "Revenue from Contract with Customer excl. Assessed Tax",
        "units": {"USD": rows},
    }


class TestTenantGuard:
    async def test_sec_financials_requires_tenant(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await public_data.sec_financials("AAPL", "revenue", headers=None)

    async def test_wikipedia_pageviews_requires_tenant(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await public_data.wikipedia_pageviews("Python", "20261101", "20261103", headers=None)


class TestSecFinancials:
    @respx.mock
    async def test_ticker_resolves_cik_then_fetches_concept(self):
        ticker_route = respx.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "0": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
                    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                },
            )
        )
        concept_route = respx.get(CONCEPT_URL).mock(
            return_value=httpx.Response(200, json=_concept_payload())
        )

        data = _json(await public_data.sec_financials("aapl", "revenue", headers=HEADERS))

        assert data["metric"] == "revenue"
        assert data["taxonomy"] == "us-gaap"
        assert data["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert data["cik"] == 320193
        assert data["entity_name"] == "Apple Inc."
        assert data["label"] == "Revenue from Contract with Customer excl. Assessed Tax"
        assert data["unit"] == "USD"
        # Most recent 8 rows, sorted by end descending.
        assert data["count"] == 8
        assert [o["end"] for o in data["observations"]] == [
            f"2025-{m:02d}-28" for m in range(10, 2, -1)
        ]
        assert data["observations"][0]["val"] == 1010
        assert ticker_route.call_count == 1
        assert concept_route.call_count == 1

    @respx.mock
    async def test_requests_carry_user_agent_header(self):
        respx.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(
                200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
            )
        )
        concept_route = respx.get(CONCEPT_URL).mock(
            return_value=httpx.Response(200, json=_concept_payload(n=1))
        )

        await public_data.sec_financials("AAPL", "revenue", headers=HEADERS)

        assert concept_route.calls[0].request.headers["User-Agent"] == (
            "AdForge/0.4 (research-agent)"
        )

    @respx.mock
    async def test_direct_cik_skips_ticker_lookup(self):
        ticker_route = respx.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json={})
        )
        concept_route = respx.get(CONCEPT_URL).mock(
            return_value=httpx.Response(200, json=_concept_payload(n=2))
        )

        data = _json(await public_data.sec_financials("320193", "revenue", headers=HEADERS))

        assert data["entity_name"] == "Apple Inc."
        assert data["count"] == 2
        assert ticker_route.call_count == 0
        # Zero-padded to 10 digits in the URL.
        assert concept_route.calls[0].request.url.path == (
            "/api/xbrl/companyconcept/CIK0000320193/us-gaap/"
            "RevenueFromContractWithCustomerExcludingAssessedTax.json"
        )

    @respx.mock
    async def test_unknown_ticker_returns_explicit_error(self):
        respx.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(
                200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
            )
        )

        data = _json(await public_data.sec_financials("NOPE", "revenue", headers=HEADERS))

        assert data["error"] == "ticker_not_found"
        assert "NOPE" in data["detail"]

    @respx.mock
    async def test_concept_404_returns_explicit_error(self):
        respx.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(
                200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
            )
        )
        respx.get(url__regex=r"https://data\.sec\.gov/api/.*").mock(
            return_value=httpx.Response(404, text='{"message": "Not Found"}')
        )

        data = _json(await public_data.sec_financials("AAPL", "revenue", headers=HEADERS))

        assert "SEC EDGAR API 404" in data["error"]
        assert "Not Found" in data["error"]


class TestWikipediaPageviews:
    @respx.mock
    async def test_success_url_encodes_article_and_totals_views(self):
        route = respx.get(PAGEVIEWS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "project": "en.wikipedia",
                            "article": "Python (programming language)",
                            "granularity": "daily",
                            "timestamp": "2026110100",
                            "views": 4107,
                        },
                        {
                            "project": "en.wikipedia",
                            "article": "Python (programming language)",
                            "granularity": "daily",
                            "timestamp": "2026110200",
                            "views": 3980,
                        },
                        {
                            "project": "en.wikipedia",
                            "article": "Python (programming language)",
                            "granularity": "daily",
                            "timestamp": "2026110300",
                            "views": 5213,
                        },
                    ]
                },
            )
        )

        data = _json(
            await public_data.wikipedia_pageviews(
                "Python (programming language)",
                "20261101",
                "20261103",
                headers=HEADERS,
            )
        )

        assert data["article"] == "Python (programming language)"
        assert data["granularity"] == "daily"
        assert data["points"] == [
            {"date": "2026110100", "views": 4107},
            {"date": "2026110200", "views": 3980},
            {"date": "2026110300", "views": 5213},
        ]
        assert data["total_views"] == 13300
        assert data["count"] == 3
        request = route.calls[0].request
        assert request.headers["User-Agent"] == "AdForge/0.4 (research-agent)"
        # Article title is percent-encoded in the path.
        assert b"Python%20%28" in request.url.raw_path

    @respx.mock
    async def test_monthly_granularity(self):
        route = respx.get(
            url__regex=r"https://wikimedia\.org/api/rest_v1/metrics/pageviews/.*"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"timestamp": "2026100100", "views": 120000}]},
            )
        )

        data = _json(
            await public_data.wikipedia_pageviews(
                "Qwen", "202608", "202610", granularity="monthly", headers=HEADERS
            )
        )

        assert data["granularity"] == "monthly"
        assert data["total_views"] == 120000
        assert route.calls[0].request.url.path.endswith(
            "/per-article/en.wikipedia/all-access/user/Qwen/monthly/20260800/20261000"
        )

    @respx.mock
    async def test_404_returns_explicit_error(self):
        respx.get(url__regex=r"https://wikimedia\.org/api/rest_v1/metrics/pageviews/.*").mock(
            return_value=httpx.Response(404, text="Not found")
        )

        data = _json(
            await public_data.wikipedia_pageviews(
                "Nonexistent Article XYZ", "20261101", "20261103", headers=HEADERS
            )
        )

        assert "Wikimedia API 404" in data["error"]
        assert "points" not in data

    async def test_settings_expose_api_bases(self):
        get_settings.cache_clear()
        s = get_settings()
        assert s.sec_edgar_api_base == "https://data.sec.gov/api"
        assert s.wikimedia_rest_api_base == "https://wikimedia.org/api/rest_v1"
