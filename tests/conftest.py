"""Pytest fixtures shared across all tests.

Test tiers
----------
* **unit** (default) — no external services. Redis is replaced by ``fakeredis``,
  the LLM by a scripted provider, and DB persistence is disabled.
* **integration** — a real embedded PostgreSQL 18 server is started for the
  session and the schema in ``scripts/sql/init.sql`` is applied. Mark the test
  with ``@pytest.mark.integration``.
* **live** — talks to a real external service. Mark with
  ``@pytest.mark.live``; skips automatically when credentials/endpoint are
  missing.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    import asyncio

    # psycopg's async connection pool cannot run on Windows' ProactorEventLoop.
    # Select the selector loop before pytest-asyncio creates any event loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure the package + test helpers are importable from a fresh checkout.
ROOT = Path(__file__).parent.parent
TESTS = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# Force test-mode env before any adforge import. Individual tests override
# these through monkeypatch when they verify other configurations.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LLM_PROVIDER", "glm")
os.environ.setdefault("ZAI_API_KEY", "test-key-not-real")
os.environ.setdefault("APP_DISABLE_GUARDRAILS", "false")
os.environ.setdefault("CHECKPOINTER_BACKEND", "memory")
os.environ.setdefault("MCP_ENABLED", "false")
os.environ.setdefault("APP_SECRET", "test-secret-not-for-production")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _sanitize_no_proxy() -> None:
    """Drop IPv6 entries from NO_PROXY inside the pytest process only.

    httpx 0.28.1 raises ``InvalidURL: Invalid port`` while parsing ANY IPv6
    entry in NO_PROXY during proxy resolution — both bare (``::1``) and
    bracketed (``[::1]``) forms — which breaks every trust_env request. The
    colon only appears in NO_PROXY entries for IPv6 addresses, so all such
    entries are removed; hostnames, IPv4 addresses/CIDRs and wildcards pass
    through untouched.
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        entries = raw.split(",")
        kept = [entry for entry in entries if ":" not in entry]
        if kept != entries:
            os.environ[key] = ",".join(kept)


def pytest_configure(config: pytest.Config) -> None:
    _sanitize_no_proxy()
    config.addinivalue_line("markers", "integration: requires embedded PostgreSQL")
    config.addinivalue_line("markers", "live: calls a real external service")


# --------------------------------------------------------------------------- #
# Per-test isolation (all tests)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
async def _test_env(monkeypatch, request):
    """Reset singletons + install fakes for every test.

    Integration tests keep the real DB session factory; unit tests get a
    fail-fast factory so a stray persistence call cannot hang on a connection
    to a non-existent localhost database.
    """
    import fakeredis.aioredis

    import adforge.graph.state as graph_state
    import adforge.guardrails.audit_log as audit_log_module
    import adforge.guardrails.rate_limiter as rate_limiter_module
    import adforge.storage.redis_client as redis_module
    import adforge.storage.session as session_module
    from adforge.config import get_settings
    from adforge.llm.factory import get_llm

    is_live = request.node.get_closest_marker("live") is not None
    for key, value in {
        "APP_ENV": "test",
        "APP_DISABLE_GUARDRAILS": "false",
        "LLM_PROVIDER": "glm",
        "ZAI_API_KEY": "test-key-not-real",
        "CHECKPOINTER_BACKEND": "memory",
        "MCP_ENABLED": "true" if is_live else "false",
        "APP_SECRET": "test-secret-not-for-production",
        "APP_REVIEWER_ALLOWLIST": "",
    }.items():
        monkeypatch.setenv(key, value)
    # Keep unit/integration tests independent of a developer's local .env.
    # Live tests are exempt so they can exercise the configured endpoint.
    if not is_live:
        for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
            monkeypatch.setenv(key, "")

    get_settings.cache_clear()
    get_llm.cache_clear()
    graph_state.reset_compiled_graph()
    audit_log_module._audit_log = None
    rate_limiter_module._limiters.clear()

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake_redis, raising=False)

    is_integration = (
        "db_env" in request.fixturenames
        or request.node.get_closest_marker("integration") is not None
    )
    if not is_integration:

        def _no_database():
            raise RuntimeError("database access is disabled in unit tests")

        monkeypatch.setattr(session_module, "get_session_factory", _no_database)

    yield

    with contextlib.suppress(Exception):
        await fake_redis.aclose()
    get_settings.cache_clear()
    get_llm.cache_clear()
    graph_state.reset_compiled_graph()
    audit_log_module._audit_log = None
    rate_limiter_module._limiters.clear()


@pytest.fixture
def sample_brief_dict():
    return {
        "product_name": "Acme SaaS",
        "product_description": (
            "Project management tool for fast-moving dev teams. Tight "
            "integrations, instant sprint burndowns, AI-assisted standup summaries."
        ),
        "target_audience": (
            "Engineering managers at 50-500 person SaaS companies in North America."
        ),
        "budget_usd": 25000.0,
        "duration_days": 30,
        "business_objective": "conversion",
        "channels_hint": ["google", "linkedin"],
        "constraints": ["No comparison-brand keyword bidding"],
    }


@pytest.fixture
def scripted_llm():
    """A deterministic LLM that answers each agent with schema-valid JSON."""
    from support import ScriptedLLM, default_agent_responses

    return ScriptedLLM(responses=default_agent_responses())


@pytest.fixture
def graph_llm(scripted_llm, monkeypatch):
    """Inject the scripted LLM into every graph-node agent singleton.

    The KPI node is deterministic and calls the real ad-platform HTTP clients.
    Integration tests have a database but no ad credentials, so the fetch is
    replaced with deterministic rows; persistence stays real so the KPI tables
    and audit trail are exercised.
    """
    from datetime import UTC, datetime, timedelta

    import adforge.agents.kpi_analytics.agent as kpi_agent_module
    import adforge.graph.state as graph_state
    from adforge.agents.kpi_analytics.fetcher import build_snapshot

    today = datetime.now(UTC).date()

    async def fake_fetch(scope, channel, campaign_id, **kwargs):
        return [
            build_snapshot(
                channel=channel,
                campaign_id=campaign_id,
                campaign_external_id=kwargs.get("external_id") or f"{channel}-1",
                platform_date=today - timedelta(days=6 - offset),
                impressions=1_000,
                clicks=50,
                spend_usd=100.0,
                conversions=5,
                revenue_usd=300.0,
                source=f"{channel}_ads",
            )
            for offset in range(7)
        ]

    monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", fake_fetch)

    for agent in (
        graph_state._research,
        graph_state._creative,
        graph_state._media_planner,
        graph_state._compliance,
        graph_state._kpi,
        graph_state._roi,
        graph_state._optimization,
    ):
        monkeypatch.setattr(agent, "_llm", scripted_llm, raising=False)
    return scripted_llm


# --------------------------------------------------------------------------- #
# Embedded PostgreSQL (integration tier)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """Start an embedded PostgreSQL server once for the whole test session."""
    embedded_postgres = pytest.importorskip(
        "embedded_postgres", reason="embedded-postgres is required for integration tests"
    )
    pgdata = tmp_path_factory.mktemp("embedded_pg") / "data"
    server = embedded_postgres.get_server(pgdata, cleanup_mode="delete")
    init_sql = (ROOT / "scripts" / "sql" / "init.sql").read_text(encoding="utf-8")
    server.psql("\\set ON_ERROR_STOP on\n" + init_sql)
    try:
        yield server
    finally:
        server.cleanup()


@pytest.fixture
async def db_env(pg_server, monkeypatch):
    """Point the app at the embedded test database and start a fresh engine."""
    from sqlalchemy import text

    import adforge.graph.state as graph_state
    import adforge.storage.session as session_module
    from adforge.config import get_settings

    async_uri = pg_server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_uri)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    get_settings.cache_clear()

    await session_module.dispose_engine()
    session_module.init_engine()
    graph_state.reset_compiled_graph()

    factory = session_module.get_session_factory()
    async with factory() as session:
        await session.execute(
            text(
                "TRUNCATE TABLE audit_events, kpi_snapshots, agent_runs, "
                "creative_artifacts, campaigns CASCADE"
            )
        )
        await session.commit()

    try:
        yield async_uri
    finally:
        await session_module.dispose_engine()
        graph_state.reset_compiled_graph()
        get_settings.cache_clear()


@pytest.fixture
async def api_client(db_env):
    """Async HTTP client bound to the FastAPI app (no network listener)."""
    import httpx

    from adforge.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://adforge.test") as client:
        yield client


@pytest.fixture
def tenant_headers():
    def _headers(tenant: str = "test_tenant", campaign: str | None = None) -> dict[str, str]:
        headers = {"X-AdForge-Tenant": tenant}
        if campaign:
            headers["X-AdForge-Campaign"] = campaign
        return headers

    return _headers
