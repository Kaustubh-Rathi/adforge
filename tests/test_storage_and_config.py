"""Tests for configuration, storage (session/redis/seed), API lifetime and
the deterministic MCP servers (LinkedIn, ROI, ads, compliance).

Everything here is unit-level: engines, Redis clients and sessions are faked so
no external service is required.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from adforge.config import Settings, get_settings
from adforge.guardrails.exceptions import (
    ContractValidationError,
    DataIsolationError,
    GuardrailError,
    LLMTransportError,
    McpToolError,
    PolicyViolation,
    RateLimitExceeded,
)
from adforge.mcp_servers import ads, compliance, permissions, roi
from adforge.mcp_servers.ads import linkedin
from adforge.storage import redis_client as redis_module
from adforge.storage import seed as seed_module
from adforge.storage import session as session_module

# Capture the real accessor at import time: the shared autouse fixture replaces
# ``session_module.get_session_factory`` with a fail-fast stub for unit tests.
_REAL_GET_SESSION_FACTORY = session_module.get_session_factory


def _prepare_env(monkeypatch, **overrides) -> None:
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_database_url_default_composition(self):
        settings = Settings(
            database_url="",
            postgres_user="alice",
            postgres_password="s3cret",
            postgres_host="db.internal",
            postgres_port=6543,
            postgres_db="ads",
        )
        assert settings.database_url == "postgresql+asyncpg://alice:s3cret@db.internal:6543/ads"

    def test_explicit_database_url_wins(self):
        explicit = "postgresql+asyncpg://remote:pass@db.example:5432/prod"
        settings = Settings(
            database_url=explicit,
            postgres_user="ignored",
            postgres_password="ignored",
            postgres_host="ignored",
            postgres_port=1111,
            postgres_db="ignored",
        )
        assert settings.database_url == explicit

    def test_redis_url_default_without_password(self):
        settings = Settings(
            redis_url="", redis_host="cache.internal", redis_port=6380, redis_password=""
        )
        assert settings.redis_url == "redis://cache.internal:6380/0"

    def test_redis_url_default_with_password(self):
        settings = Settings(
            redis_url="", redis_host="cache.internal", redis_port=6380, redis_password="secret"
        )
        assert settings.redis_url == "redis://:secret@cache.internal:6380/0"

    def test_explicit_redis_url_wins(self):
        settings = Settings(redis_url="redis://explicit:6379/5", redis_host="ignored", redis_port=1)
        assert settings.redis_url == "redis://explicit:6379/5"

    @staticmethod
    def _safe_prod(**overrides):
        values = {
            "app_secret": "prod-secret",
            "app_reviewer_allowlist": "rev@example.com",
            "api_auth_token": "prod-key",
            "checkpointer_backend": "postgres",
            "app_cors_origins": "https://app.example.com",
        }
        values.update(overrides)
        return Settings(**values)

    def test_in_prod_only_true_for_prod(self):
        assert self._safe_prod(app_env="prod").in_prod is True
        assert self._safe_prod(app_env="staging").in_prod is False
        assert Settings(app_env="dev").in_prod is False
        assert Settings(app_env="test").in_prod is False

    def test_guardrails_active_dev_can_be_disabled(self):
        assert Settings(app_env="dev", app_disable_guardrails=True).guardrails_active is False
        assert Settings(app_env="dev", app_disable_guardrails=False).guardrails_active is True

    def test_guardrails_active_cannot_be_disabled_in_staging_or_prod(self):
        for env in ("staging", "prod"):
            with pytest.raises(ValueError, match="APP_DISABLE_GUARDRAILS"):
                self._safe_prod(app_env=env, app_disable_guardrails=True)

    def test_mcp_url_maps_every_server_and_port(self):
        settings = Settings(
            mcp_research_port=19001,
            mcp_ads_port=19002,
            mcp_analytics_port=19003,
            mcp_roi_port=19004,
            mcp_compliance_port=19005,
            mcp_google_ads_official_port=19006,
            mcp_public_data_port=19007,
            mcp_linkedin_port=19012,
        )
        expected = {
            "research": 19001,
            "ads": 19002,
            "analytics": 19003,
            "roi": 19004,
            "compliance": 19005,
            "google_ads_official": 19006,
            "public_data": 19007,
            "linkedin": 19012,
        }
        for server, port in expected.items():
            assert settings.mcp_url(server) == f"http://localhost:{port}/sse"


# --------------------------------------------------------------------------- #
# SQLAlchemy session management
# --------------------------------------------------------------------------- #


class FakeAsyncSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeSessionFactory:
    def __init__(self, session: FakeAsyncSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


class TestSessionModule:
    def test_init_engine_creates_engine_and_factory(self, monkeypatch):
        fake_engine = object()
        fake_factory = object()
        create_calls: list[tuple] = []
        maker_calls: list[tuple] = []

        def fake_create_async_engine(url, **kwargs):
            create_calls.append((url, kwargs))
            return fake_engine

        def fake_async_sessionmaker(engine, **kwargs):
            maker_calls.append((engine, kwargs))
            return fake_factory

        monkeypatch.setattr(session_module, "create_async_engine", fake_create_async_engine)
        monkeypatch.setattr(session_module, "async_sessionmaker", fake_async_sessionmaker)
        monkeypatch.setattr(session_module, "_engine", None)
        monkeypatch.setattr(session_module, "_session_factory", None)

        session_module.init_engine(echo=True)

        assert len(create_calls) == 1
        url, kwargs = create_calls[0]
        assert url == get_settings().database_url
        assert kwargs == {
            "echo": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_pre_ping": True,
        }
        assert maker_calls == [(fake_engine, {"class_": AsyncSession, "expire_on_commit": False})]
        assert session_module._engine is fake_engine
        assert session_module._session_factory is fake_factory

    def test_get_session_factory_initializes_when_missing(self, monkeypatch):
        sentinel = object()
        init_calls: list[bool] = []

        def fake_init_engine() -> None:
            init_calls.append(True)
            session_module._session_factory = sentinel

        monkeypatch.setattr(session_module, "_session_factory", None)
        monkeypatch.setattr(session_module, "init_engine", fake_init_engine)
        monkeypatch.setattr(session_module, "get_session_factory", _REAL_GET_SESSION_FACTORY)

        assert session_module.get_session_factory() is sentinel
        assert init_calls == [True]

    def test_get_session_factory_reuses_existing_factory(self, monkeypatch):
        sentinel = object()

        def fail_init_engine() -> None:
            raise AssertionError("init_engine must not be called")

        monkeypatch.setattr(session_module, "_session_factory", sentinel)
        monkeypatch.setattr(session_module, "init_engine", fail_init_engine)
        monkeypatch.setattr(session_module, "get_session_factory", _REAL_GET_SESSION_FACTORY)

        assert session_module.get_session_factory() is sentinel

    async def test_session_scope_commits_on_success(self, monkeypatch):
        session = FakeAsyncSession()
        monkeypatch.setattr(
            session_module, "get_session_factory", lambda: FakeSessionFactory(session)
        )

        async with session_module.session_scope() as yielded:
            assert yielded is session

        assert session.commits == 1
        assert session.rollbacks == 0
        assert session.entered is True
        assert session.exited is True

    async def test_session_scope_rolls_back_and_reraises(self, monkeypatch):
        session = FakeAsyncSession()
        monkeypatch.setattr(
            session_module, "get_session_factory", lambda: FakeSessionFactory(session)
        )

        with pytest.raises(RuntimeError, match="boom"):
            async with session_module.session_scope():
                raise RuntimeError("boom")

        assert session.commits == 0
        assert session.rollbacks == 1
        assert session.exited is True

    async def test_dispose_engine_disposes_and_resets_globals(self, monkeypatch):
        class FakeEngine:
            def __init__(self) -> None:
                self.dispose_calls = 0

            async def dispose(self) -> None:
                self.dispose_calls += 1

        engine = FakeEngine()
        monkeypatch.setattr(session_module, "_engine", engine)
        monkeypatch.setattr(session_module, "_session_factory", object())

        await session_module.dispose_engine()

        assert engine.dispose_calls == 1
        assert session_module._engine is None
        assert session_module._session_factory is None

    async def test_dispose_engine_when_none_is_noop(self, monkeypatch):
        monkeypatch.setattr(session_module, "_engine", None)
        monkeypatch.setattr(session_module, "_session_factory", object())

        await session_module.dispose_engine()

        assert session_module._engine is None
        assert session_module._session_factory is None


# --------------------------------------------------------------------------- #
# Redis singleton
# --------------------------------------------------------------------------- #


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestRedisClient:
    async def test_get_redis_caches_singleton(self, monkeypatch):
        calls: list[tuple[str, dict]] = []
        fake = FakeRedis()

        def fake_from_url(url, **kwargs):
            calls.append((url, kwargs))
            return fake

        monkeypatch.setattr(redis_module, "_redis", None)
        monkeypatch.setattr(redis_module.redis, "from_url", fake_from_url)

        first = await redis_module.get_redis()
        second = await redis_module.get_redis()

        assert first is fake
        assert second is fake
        assert len(calls) == 1
        url, kwargs = calls[0]
        assert url == get_settings().redis_url
        assert kwargs == {"encoding": "utf-8", "decode_responses": True}

    async def test_get_redis_async_is_an_alias(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(redis_module, "_redis", None)
        monkeypatch.setattr(redis_module.redis, "from_url", lambda url, **kwargs: fake)

        assert await redis_module.get_redis_async() is fake
        assert await redis_module.get_redis() is fake

    async def test_close_redis_closes_and_resets(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(redis_module, "_redis", fake)

        await redis_module.close_redis()

        assert fake.closed is True
        assert redis_module._redis is None

    async def test_close_redis_when_none_is_noop(self, monkeypatch):
        monkeypatch.setattr(redis_module, "_redis", None)

        await redis_module.close_redis()

        assert redis_module._redis is None


# --------------------------------------------------------------------------- #
# Seed script
# --------------------------------------------------------------------------- #


class RecordingSession:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict | None]] = []
        self.commit_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None) -> None:
        self.executions.append((str(statement), params))

    async def commit(self) -> None:
        self.commit_count += 1


class RecordingFactory:
    def __init__(self, session: RecordingSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


class TestSeed:
    @staticmethod
    def _examples_file(tmp_path):
        examples = [
            {
                "name": "Seed Campaign",
                "tenant_id": "tenant_seed",
                "days_live": 2,
                "avg_conversion_value": 50.0,
                "brief": {
                    "product_name": "Seed Product",
                    "channels_hint": ["google", "meta"],
                },
            }
        ]
        path = tmp_path / "campaigns.json"
        path.write_text(json.dumps(examples), encoding="utf-8")
        return path

    async def test_seed_inserts_campaign_and_kpis_then_disposes(self, monkeypatch, tmp_path):
        session = RecordingSession()
        calls = {"init": 0, "dispose": 0}

        def fake_init_engine() -> None:
            calls["init"] += 1

        async def fake_dispose_engine() -> None:
            calls["dispose"] += 1

        monkeypatch.setattr(seed_module, "EXAMPLES_FILE", self._examples_file(tmp_path))
        monkeypatch.setattr(seed_module.session_module, "init_engine", fake_init_engine)
        monkeypatch.setattr(seed_module.session_module, "dispose_engine", fake_dispose_engine)
        monkeypatch.setattr(
            seed_module.session_module,
            "get_session_factory",
            lambda: RecordingFactory(session),
        )

        await seed_module.seed()

        statements = [sql for sql, _ in session.executions]
        assert sum("INSERT INTO campaigns" in sql for sql in statements) == 1
        # 2 channels * 2 days_live
        assert sum("INSERT INTO kpi_snapshots" in sql for sql in statements) == 4
        assert any("UPDATE campaigns" in sql for sql in statements)
        assert session.commit_count == 1
        assert calls == {"init": 1, "dispose": 1}

        campaign_params = next(
            params for sql, params in session.executions if "INSERT INTO campaigns" in sql
        )
        assert campaign_params is not None
        assert campaign_params["tid"] == "tenant_seed"
        assert campaign_params["name"] == "Seed Campaign"

        kpi_channels = {
            params["ch"] for sql, params in session.executions if "INSERT INTO kpi_snapshots" in sql
        }
        assert kpi_channels == {"google", "meta"}

    def test_run_calls_seed_via_asyncio(self, monkeypatch):
        ran: list[str] = []

        async def fake_seed() -> None:
            ran.append("seed")

        monkeypatch.setattr(seed_module, "seed", fake_seed)

        seed_module.run()

        assert ran == ["seed"]


# --------------------------------------------------------------------------- #
# API lifespan + entry point
# --------------------------------------------------------------------------- #


class TestApiMain:
    async def test_lifespan_initialises_and_cleans_up(self, monkeypatch):
        from adforge.api import main as api_main

        _prepare_env(monkeypatch, APP_ENV="test")
        events: list[tuple[str, object]] = []

        def fake_init_engine(echo: bool = False) -> None:
            events.append(("init", echo))

        async def fake_dispose_engine() -> None:
            events.append(("dispose", None))

        async def fake_close_redis() -> None:
            events.append(("close_redis", None))

        monkeypatch.setattr(api_main, "init_engine", fake_init_engine)
        monkeypatch.setattr(api_main, "dispose_engine", fake_dispose_engine)
        monkeypatch.setattr(api_main, "close_redis", fake_close_redis)

        async with api_main.lifespan(api_main.app):
            assert events == [("init", False)]

        assert events == [
            ("init", False),
            ("dispose", None),
            ("close_redis", None),
        ]

    @pytest.mark.parametrize(
        ("app_env", "expected_reload"),
        [("dev", True), ("test", False), ("prod", False)],
    )
    def test_run_passes_host_port_and_reload(self, monkeypatch, app_env, expected_reload):
        from adforge.api import main as api_main

        env = {"APP_ENV": app_env, "API_HOST": "127.0.0.2", "API_PORT": "8123"}
        if app_env in ("staging", "prod"):
            env.update(
                APP_SECRET="prod-secret",
                APP_REVIEWER_ALLOWLIST="rev@example.com",
                API_AUTH_TOKEN="prod-key",
                CHECKPOINTER_BACKEND="postgres",
                APP_CORS_ORIGINS="https://app.example.com",
            )
        _prepare_env(monkeypatch, **env)
        calls: list[tuple[tuple, dict]] = []

        def fake_run(*args, **kwargs) -> None:
            calls.append((args, kwargs))

        monkeypatch.setattr(api_main.uvicorn, "run", fake_run)

        api_main.run()

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ("adforge.api.main:app",)
        assert kwargs["host"] == "127.0.0.2"
        assert kwargs["port"] == 8123
        assert kwargs["reload"] is expected_reload


# --------------------------------------------------------------------------- #
# LinkedIn MCP
# --------------------------------------------------------------------------- #


class TestLinkedInMcp:
    async def test_missing_token_returns_explicit_error(self, monkeypatch):
        _prepare_env(monkeypatch, LINKEDIN_ACCESS_TOKEN="")

        result = await linkedin.get_ad_analytics(
            ad_account_id="act_123", days_back=3, headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)

        assert data["error"] == "linkedin_not_configured"
        assert "LINKEDIN_ACCESS_TOKEN" in data["detail"]

    async def test_campaign_id_is_required(self, monkeypatch):
        _prepare_env(monkeypatch, LINKEDIN_ACCESS_TOKEN="live-token")

        result = await linkedin.get_ad_analytics(
            ad_account_id="act_123", headers={"X-AdForge-Tenant": "acme"}
        )
        data = json.loads(result)

        assert data["error"] == "campaign_id_required"

    async def test_missing_tenant_raises(self):
        with pytest.raises(ValueError, match="X-AdForge-Tenant"):
            await linkedin.get_ad_analytics(ad_account_id="act_123", headers=None)

    @respx.mock
    async def test_live_reporting_returns_daily_rows(self, monkeypatch):
        _prepare_env(
            monkeypatch,
            LINKEDIN_ACCESS_TOKEN="live-token",
            LINKEDIN_AD_ACCOUNT_ID="act_123",
        )
        respx.get("https://api.linkedin.com/rest/adAnalytics").mock(
            return_value=httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "dateRange": {"start": {"year": 2026, "month": 1, "day": 2}},
                            "impressions": 100,
                            "clicks": 10,
                            "costInLocalCurrency": "5.0",
                            "externalWebsiteConversions": 1,
                            "conversionValueInLocalCurrency": "20.0",
                        }
                    ]
                },
            )
        )
        result = await linkedin.get_ad_analytics(
            ad_account_id="act_123",
            campaign_ids=["777"],
            days_back=3,
            headers={"X-AdForge-Tenant": "acme"},
        )
        data = json.loads(result)

        assert data["ad_account_id"] == "act_123"
        assert data["count"] == 1
        assert data["daily"][0]["channel"] == "linkedin"
        assert data["daily"][0]["source"] == "linkedin_ads"
        assert data["errors"] == []

    @respx.mock
    async def test_reporting_error_is_reported_per_campaign(self, monkeypatch):
        _prepare_env(monkeypatch, LINKEDIN_ACCESS_TOKEN="live-token")
        respx.get("https://api.linkedin.com/rest/adAnalytics").mock(
            return_value=httpx.Response(500, text="down")
        )
        result = await linkedin.get_ad_analytics(
            ad_account_id="act_123",
            campaign_ids=["777"],
            days_back=3,
            headers={"X-AdForge-Tenant": "acme"},
        )
        data = json.loads(result)

        assert data["count"] == 0
        assert data["errors"]
        assert "adAnalytics failed" in data["errors"][0]


# --------------------------------------------------------------------------- #
# ROI MCP
# --------------------------------------------------------------------------- #


class TestRoiMcp:
    async def test_compute_cpa_success(self):
        data = json.loads(await roi.compute_cpa(spend_usd=100.0, conversions=4))
        assert data["cpa"] == 25.0

    async def test_compute_cpa_zero_conversions(self):
        data = json.loads(await roi.compute_cpa(spend_usd=100.0, conversions=0))
        assert "error" in data

    async def test_compute_cpa_negative_conversions(self):
        data = json.loads(await roi.compute_cpa(spend_usd=100.0, conversions=-1))
        assert "error" in data

    async def test_compute_payback_period_success(self):
        data = json.loads(
            await roi.compute_payback_period(cac_usd=100.0, ltv_usd=1000.0, gross_margin_pct=50.0)
        )
        # daily profit = 1000 * 0.5 / 365; 100 / that = 73.0 days
        assert data["payback_period_days"] == pytest.approx(73.0)

    async def test_compute_payback_period_invalid_ltv(self):
        data = json.loads(
            await roi.compute_payback_period(cac_usd=100.0, ltv_usd=0.0, gross_margin_pct=50.0)
        )
        assert "error" in data

    async def test_compute_payback_period_invalid_margin(self):
        data = json.loads(
            await roi.compute_payback_period(cac_usd=100.0, ltv_usd=1000.0, gross_margin_pct=0.0)
        )
        assert "error" in data

    @pytest.mark.parametrize(
        ("roas", "payback_days", "expected"),
        [
            (4.0, 29.0, "scale"),
            (4.0, None, "scale"),
            (4.0, 30.0, "optimize"),
            (2.0, 59.0, "optimize"),
            (2.0, 60.0, "pause"),
            (1.0, None, "pause"),
            (0.99, None, "kill"),
        ],
    )
    async def test_classify_recommendation_boundaries(self, roas, payback_days, expected):
        data = json.loads(await roi.classify_recommendation(roas=roas, payback_days=payback_days))
        assert data["recommendation"] == expected
        assert data["roas"] == roas
        assert data["payback_days"] == payback_days


# --------------------------------------------------------------------------- #
# Ads MCP (write approval + channels)
# --------------------------------------------------------------------------- #


def _approval_headers(campaign_id: str, operation: str) -> dict[str, str]:
    token = permissions.issue_approval_token(
        campaign_id, operation, "rev@example.com", tenant_id="acme"
    )
    return {
        "X-AdForge-Tenant": "acme",
        "X-AdForge-Campaign": campaign_id,
        "X-AdForge-Approval-Token": token,
    }


class TestAdsMcp:
    @pytest.fixture(autouse=True)
    def _write_gateway(self, monkeypatch):
        monkeypatch.setenv("ADFORGE_ENABLE_LIVE_WRITES", "true")
        monkeypatch.setenv("ADFORGE_PUBLISH_WEBHOOK_URL", "https://ads.example.test/hook")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    async def test_publish_without_approval_token_is_blocked(self):
        campaign_id = str(uuid4())
        result = await ads.publish_campaign(
            channel="google",
            campaign_name="No approval",
            budget_usd=500.0,
            creative={"headline": "Test"},
            audience={"age": "25-44"},
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Campaign": campaign_id,
            },
        )
        data = json.loads(result)

        assert data["error"] == "approval_required"
        assert data["operation"] == "publish_campaign"
        assert data["campaign_id"] == campaign_id

    @pytest.mark.parametrize("channel", ["google", "linkedin"])
    @respx.mock
    async def test_publish_with_valid_approval_succeeds(self, channel):
        campaign_id = str(uuid4())
        route = respx.post("https://ads.example.test/hook").mock(
            return_value=httpx.Response(
                200,
                json={
                    "external_campaign_id": f"{channel}-ext-1",
                    "status": "active",
                },
            )
        )
        result = await ads.publish_campaign(
            channel=channel,
            campaign_name="Approved campaign",
            budget_usd=500.0,
            creative={"headline": "Test"},
            audience={"age": "25-44"},
            headers=_approval_headers(campaign_id, "publish_campaign"),
        )
        data = json.loads(result)

        assert data["channel"] == channel
        assert data["external_campaign_id"] == f"{channel}-ext-1"
        assert data["status"] == "active"
        assert data["published_with_approval"] is True
        body = json.loads(route.calls[0].request.content)
        assert body["action"] == "publish_campaign"
        assert body["channel"] == channel

    @respx.mock
    async def test_publish_adapter_without_external_id_fails(self):
        campaign_id = str(uuid4())
        respx.post("https://ads.example.test/hook").mock(
            return_value=httpx.Response(200, json={"status": "active"})
        )
        result = await ads.publish_campaign(
            channel="google",
            campaign_name="Approved campaign",
            budget_usd=500.0,
            creative={},
            audience={},
            headers=_approval_headers(campaign_id, "publish_campaign"),
        )
        data = json.loads(result)

        assert data["error"] == "publish_failed"
        assert "external_campaign_id" in data["detail"]

    @pytest.mark.parametrize("budget", [0, -10.0])
    async def test_publish_non_positive_budget_errors(self, budget):
        campaign_id = str(uuid4())
        result = await ads.publish_campaign(
            channel="google",
            campaign_name="Bad budget",
            budget_usd=budget,
            creative={},
            audience={},
            headers=_approval_headers(campaign_id, "publish_campaign"),
        )
        assert "budget must be positive" in json.loads(result)["detail"]

    @respx.mock
    async def test_pause_campaign_with_valid_approval(self):
        campaign_id = str(uuid4())
        respx.post("https://ads.example.test/hook").mock(
            return_value=httpx.Response(200, json={"status": "paused"})
        )
        result = await ads.pause_campaign(
            channel="google",
            external_campaign_id="google_cpg_123",
            headers=_approval_headers(campaign_id, "pause_campaign"),
        )
        data = json.loads(result)

        assert data["status"] == "paused"
        assert data["paused_with_approval"] is True
        assert data["external_campaign_id"] == "google_cpg_123"

    @respx.mock
    async def test_update_budget_with_valid_approval(self):
        campaign_id = str(uuid4())
        respx.post("https://ads.example.test/hook").mock(
            return_value=httpx.Response(200, json={"status": "updated"})
        )
        result = await ads.update_budget(
            channel="meta",
            external_campaign_id="meta_cpg_123",
            new_budget_usd=250.0,
            headers=_approval_headers(campaign_id, "update_budget"),
        )
        data = json.loads(result)

        assert data["status"] == "updated"
        assert data["daily_budget_usd"] == 250.0
        assert data["updated_with_approval"] is True

    @pytest.mark.parametrize("budget", [0, -1.0])
    async def test_update_budget_non_positive_errors(self, budget):
        campaign_id = str(uuid4())
        result = await ads.update_budget(
            channel="meta",
            external_campaign_id="meta_cpg_123",
            new_budget_usd=budget,
            headers=_approval_headers(campaign_id, "update_budget"),
        )
        assert "budget must be positive" in json.loads(result)["detail"]

    async def test_list_channels_returns_four(self):
        data = json.loads(await ads.list_channels())
        assert len(data["channels"]) == 4
        assert {c["id"] for c in data["channels"]} == {
            "google",
            "meta",
            "linkedin",
            "tiktok",
        }


# --------------------------------------------------------------------------- #
# Compliance MCP
# --------------------------------------------------------------------------- #


class TestComplianceMcp:
    async def test_financial_claim_requires_disclosure(self):
        result = await compliance.evaluate_creative(
            "Guaranteed return on your crypto investment",
            headers={"X-AdForge-Tenant": "acme"},
        )
        data = json.loads(result)

        assert data["approved"] is True
        assert data["risk_level"] == "high"
        assert data["required_disclosures"]
        assert any("Investing involves risk" in d for d in data["required_disclosures"])
        assert any("DISCLOSE_FINANCIAL" in issue for issue in data["flagged_issues"])

    async def test_clean_creative_is_approved(self):
        result = await compliance.evaluate_creative(
            "Build better software with Acme SaaS. Start free.",
            headers={"X-AdForge-Tenant": "acme"},
        )
        data = json.loads(result)

        assert data["approved"] is True
        assert data["risk_level"] == "low"
        assert data["blocked_terms"] == []

    async def test_missing_tenant_returns_error(self):
        data = json.loads(await compliance.evaluate_creative("Clean copy", headers=None))
        assert data == {"error": "missing tenant header"}

    async def test_list_blocked_terms(self):
        data = json.loads(await compliance.list_blocked_terms(headers={"X-AdForge-Tenant": "acme"}))
        rule_ids = {rule["rule_id"] for rule in data["blocked"]}

        assert len(data["blocked"]) >= 4
        assert "BLOCK_TOBACCO" in rule_ids
        assert all({"rule_id", "pattern", "description"} <= set(r) for r in data["blocked"])


# --------------------------------------------------------------------------- #
# Guardrail exceptions
# --------------------------------------------------------------------------- #


class TestGuardrailExceptions:
    def test_contract_validation_error(self):
        errors = [{"loc": ("headline",), "msg": "too short"}]
        exc = ContractValidationError("creative", errors)
        assert isinstance(exc, GuardrailError)
        assert exc.agent_name == "creative"
        assert exc.errors == errors
        assert "creative" in str(exc)
        assert "contract validation failed" in str(exc)

    def test_data_isolation_error(self):
        exc = DataIsolationError("acme", "campaign:abc")
        assert isinstance(exc, GuardrailError)
        assert exc.tenant_id == "acme"
        assert exc.attempted_resource == "campaign:abc"
        assert "data-isolation" in str(exc)
        assert "acme" in str(exc)

    def test_rate_limit_exceeded(self):
        exc = RateLimitExceeded("llm:acme", 10, 60)
        assert isinstance(exc, GuardrailError)
        assert exc.scope == "llm:acme"
        assert exc.limit == 10
        assert exc.window_seconds == 60
        assert "rate-limit" in str(exc)
        assert "10 calls" in str(exc)

    def test_policy_violation(self):
        exc = PolicyViolation("BLOCK_TOBACCO", "tobacco is prohibited")
        assert isinstance(exc, GuardrailError)
        assert exc.rule_id == "BLOCK_TOBACCO"
        assert exc.detail == "tobacco is prohibited"
        assert str(exc) == "[policy:BLOCK_TOBACCO] tobacco is prohibited"

    def test_llm_transport_error(self):
        exc = LLMTransportError("gateway timeout")
        assert isinstance(exc, GuardrailError)
        assert str(exc) == "gateway timeout"

    def test_mcp_tool_error(self):
        exc = McpToolError("research", "web_search", "timed out")
        assert isinstance(exc, GuardrailError)
        assert exc.server == "research"
        assert exc.tool == "web_search"
        assert exc.detail == "timed out"
        assert str(exc) == "[mcp:research/web_search] timed out"
