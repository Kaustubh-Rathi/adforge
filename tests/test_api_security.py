"""Security-focused API tests: auth, CORS, allowlists, publish gating.

These exercise the FastAPI surface with dependency overrides so no database is
required. The publish endpoint is the only path that can mark a campaign live,
so its failure modes get explicit coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI, HTTPException

import adforge.api.main as api_main
from adforge.api import deps
from adforge.api.routes import reviews
from adforge.config import Settings, get_settings
from adforge.mcp_servers import permissions


def _prepare_env(monkeypatch, **overrides) -> None:
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# API key authentication
# --------------------------------------------------------------------------- #


class TestApiKeyAuth:
    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()

        @app.get("/whoami")
        async def whoami(tenant: str = Depends(deps.get_tenant)) -> dict[str, str]:
            return {"tenant": tenant}

        return app

    async def _get(self, app: FastAPI, headers: dict[str, str]):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adforge.test") as client:
            return await client.get("/whoami", headers=headers)

    async def test_key_required_when_configured(self, monkeypatch):
        _prepare_env(monkeypatch, API_AUTH_TOKEN="s3cret")
        r = await self._get(self._app(), {"X-AdForge-Tenant": "acme"})
        assert r.status_code == 401
        assert "X-API-Key" in r.json()["detail"]

    async def test_wrong_key_rejected(self, monkeypatch):
        _prepare_env(monkeypatch, API_AUTH_TOKEN="s3cret")
        r = await self._get(
            self._app(),
            {"X-AdForge-Tenant": "acme", "X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    async def test_correct_key_allows_tenant(self, monkeypatch):
        _prepare_env(monkeypatch, API_AUTH_TOKEN="s3cret")
        r = await self._get(
            self._app(),
            {"X-AdForge-Tenant": "acme", "X-API-Key": "s3cret"},
        )
        assert r.status_code == 200
        assert r.json() == {"tenant": "acme"}

    async def test_blank_token_keeps_dev_mode_open(self, monkeypatch):
        _prepare_env(monkeypatch, API_AUTH_TOKEN="")
        r = await self._get(self._app(), {"X-AdForge-Tenant": "acme"})
        assert r.status_code == 200

    async def test_tenant_still_required_with_valid_key(self, monkeypatch):
        _prepare_env(monkeypatch, API_AUTH_TOKEN="s3cret")
        r = await self._get(self._app(), {"X-API-Key": "s3cret"})
        assert r.status_code == 400
        assert "X-AdForge-Tenant" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #


class TestCors:
    async def _preflight(self, origin: str):
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adforge.test") as client:
            return await client.options(
                "/health",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-API-Key",
                },
            )

    async def test_configured_origin_is_allowed(self):
        # The default development origin configured in Settings.
        r = await self._preflight("http://localhost:8501")
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:8501"
        assert "X-API-Key" in r.headers["access-control-allow-headers"]

    async def test_unconfigured_origin_is_not_allowed(self):
        r = await self._preflight("https://evil.example")
        assert "access-control-allow-origin" not in r.headers

    def test_wildcard_is_rejected_by_production_validator(self):
        with pytest.raises(ValueError, match="APP_CORS_ORIGINS"):
            Settings(
                app_env="prod",
                app_secret="s",
                app_reviewer_allowlist="rev@example.com",
                api_auth_token="k",
                checkpointer_backend="postgres",
                app_cors_origins="*",
            )


# --------------------------------------------------------------------------- #
# Reviewer allowlist
# --------------------------------------------------------------------------- #


class TestReviewerAllowlist:
    def test_unknown_reviewer_is_403(self, monkeypatch):
        _prepare_env(monkeypatch, APP_REVIEWER_ALLOWLIST="rev@acme.test, lead@acme.test")
        with pytest.raises(HTTPException) as exc:
            reviews._assert_reviewer_allowed("intern@acme.test")
        assert exc.value.status_code == 403
        assert "not in allowlist" in exc.value.detail

    def test_missing_reviewer_is_403_when_allowlist_set(self, monkeypatch):
        _prepare_env(monkeypatch, APP_REVIEWER_ALLOWLIST="rev@acme.test")
        with pytest.raises(HTTPException) as exc:
            reviews._assert_reviewer_allowed(None)
        assert exc.value.status_code == 403

    def test_allowlist_match_is_case_insensitive(self, monkeypatch):
        _prepare_env(monkeypatch, APP_REVIEWER_ALLOWLIST="Rev@Acme.Test")
        reviews._assert_reviewer_allowed("rev@acme.test")

    def test_empty_allowlist_is_dev_mode(self, monkeypatch):
        _prepare_env(monkeypatch, APP_REVIEWER_ALLOWLIST="")
        reviews._assert_reviewer_allowed("anyone@example.com")


# --------------------------------------------------------------------------- #
# Production safety validator
# --------------------------------------------------------------------------- #


class TestProductionSafetyValidator:
    @staticmethod
    def _safe(**overrides):
        values = {
            "app_env": "prod",
            "app_secret": "prod-secret",
            "app_reviewer_allowlist": "rev@example.com",
            "api_auth_token": "prod-key",
            "checkpointer_backend": "postgres",
            "app_cors_origins": "https://app.example.com",
        }
        values.update(overrides)
        return Settings(**values)

    def test_safe_production_config_starts(self):
        settings = self._safe()
        assert settings.in_prod is True
        assert settings.guardrails_active is True

    @pytest.mark.parametrize(
        ("override", "needle"),
        [
            ({"app_secret": ""}, "APP_SECRET"),
            ({"app_reviewer_allowlist": ""}, "APP_REVIEWER_ALLOWLIST"),
            ({"api_auth_token": ""}, "API_AUTH_TOKEN"),
            (
                {"guardrails_require_compliance_approval": False},
                "GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL",
            ),
            ({"checkpointer_backend": "none"}, "CHECKPOINTER_BACKEND"),
            ({"app_disable_guardrails": True}, "APP_DISABLE_GUARDRAILS"),
            ({"app_cors_origins": "*"}, "APP_CORS_ORIGINS"),
        ],
    )
    def test_unsafe_production_config_refuses_to_start(self, override, needle):
        with pytest.raises(ValueError, match=needle):
            self._safe(**override)

    def test_staging_is_held_to_the_same_standard(self):
        with pytest.raises(ValueError, match="APP_SECRET"):
            self._safe(app_env="staging", app_secret="")


# --------------------------------------------------------------------------- #
# Single-use, tenant- and reviewer-bound approval tokens
# --------------------------------------------------------------------------- #


class TestApprovalConsumption:
    async def test_token_is_single_use(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id,
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        claims = await permissions.verify_and_consume_approval(
            token, campaign_id, "publish_campaign", tenant_id="acme"
        )
        assert claims is not None
        assert claims["tenant_id"] == "acme"

        second = await permissions.verify_and_consume_approval(
            token, campaign_id, "publish_campaign", tenant_id="acme"
        )
        assert second is None

    async def test_wrong_tenant_does_not_consume(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id,
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        assert (
            await permissions.verify_and_consume_approval(
                token, campaign_id, "publish_campaign", tenant_id="other"
            )
            is None
        )
        # A failed scope check must not burn the legitimate token.
        assert (
            await permissions.verify_and_consume_approval(
                token, campaign_id, "publish_campaign", tenant_id="acme"
            )
            is not None
        )

    async def test_wrong_reviewer_does_not_consume(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id,
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        assert (
            await permissions.verify_and_consume_approval(
                token,
                campaign_id,
                "publish_campaign",
                tenant_id="acme",
                reviewer_email="other@example.com",
            )
            is None
        )
        assert (
            await permissions.verify_and_consume_approval(
                token,
                campaign_id,
                "publish_campaign",
                tenant_id="acme",
                reviewer_email="rev@example.com",
            )
            is not None
        )


# --------------------------------------------------------------------------- #
# Publish endpoint
# --------------------------------------------------------------------------- #


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeSession:
    def __init__(self, rows):
        self._rows = list(rows)
        self.statements: list[tuple[str, dict | None]] = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Result(self._rows.pop(0) if self._rows else None)


class _FakeGraph:
    def __init__(self, values):
        self.values = values

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


@pytest.fixture
async def publish_client(monkeypatch):
    """ASGI client with the DB dependency replaced by a scripted session."""
    sessions: list[_FakeSession] = []
    rows: list[_Row] = []

    async def _get_db():
        session = _FakeSession(rows)
        sessions.append(session)
        yield session

    api_main.app.dependency_overrides[deps.get_db] = _get_db
    transport = httpx.ASGITransport(app=api_main.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://adforge.test") as client:
            yield client, sessions, rows
    finally:
        api_main.app.dependency_overrides.pop(deps.get_db, None)


def _publish_state(token: str | None = None) -> dict:
    state = {
        "creative": {"headline": "Calmer sprints"},
        "media_plan": {
            "channel_allocations": [
                {"channel": "google", "budget_usd": 500.0},
                {"channel": "linkedin", "budget_usd": 250.0},
            ],
            "total_budget_usd": 750.0,
            "audience_targeting": {"age": "28-45"},
        },
        "brief": {"product_name": "Acme"},
    }
    if token is not None:
        state["approval_token"] = token
    return state


def _enable_writes(monkeypatch) -> None:
    _prepare_env(
        monkeypatch,
        APP_SECRET="test-secret-not-for-production",
        ADFORGE_ENABLE_LIVE_WRITES="true",
        ADFORGE_PUBLISH_WEBHOOK_URL="https://ads.example.test/publish",
        GUARDRAILS_MAX_CAMPAIGN_BUDGET_USD="10000",
    )


class TestPublishEndpoint:
    @respx.mock
    async def test_publish_success_moves_campaign_live(self, publish_client, monkeypatch):
        client, sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        token = permissions.issue_approval_token(
            str(campaign_id),
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        rows.extend([_Row(), _Row(name="Acme", status="approved")])

        async def fake_graph():
            return _FakeGraph(_publish_state(token))

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)
        route = respx.post("https://ads.example.test/publish").mock(
            side_effect=[
                httpx.Response(200, json={"external_campaign_id": "g-1"}),
                httpx.Response(200, json={"external_campaign_id": "li-1"}),
            ]
        )

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Reviewer": "rev@example.com",
            },
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "live"
        assert [item["external_campaign_id"] for item in body["published"]] == [
            "g-1",
            "li-1",
        ]
        assert route.call_count == 2
        assert any(
            "UPDATE campaigns SET status = 'live'" in statement
            for statement, _ in sessions[0].statements
        )
        assert any("publish.executed" in statement for statement, _ in sessions[0].statements)

    @respx.mock
    async def test_publish_twice_reuses_consumed_token(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        token = permissions.issue_approval_token(
            str(campaign_id),
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        rows.extend([_Row(), _Row(name="Acme", status="approved")])

        async def fake_graph():
            return _FakeGraph(_publish_state(token))

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)
        respx.post("https://ads.example.test/publish").mock(
            side_effect=[
                httpx.Response(200, json={"external_campaign_id": "g-1"}),
                httpx.Response(200, json={"external_campaign_id": "li-1"}),
            ]
        )
        headers = {
            "X-AdForge-Tenant": "acme",
            "X-AdForge-Reviewer": "rev@example.com",
        }

        first = await client.post(f"/reviews/{campaign_id}/publish", headers=headers)
        assert first.status_code == 200

        second = await client.post(f"/reviews/{campaign_id}/publish", headers=headers)
        assert second.status_code == 409
        assert "already used" in second.json()["detail"]

    async def test_unapproved_campaign_cannot_publish(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        rows.extend([_Row(), _Row(name="Acme", status="reviewing")])

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Reviewer": "rev@example.com",
            },
        )

        assert r.status_code == 409
        assert "must be approved" in r.json()["detail"]

    async def test_missing_approval_token_is_refused(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        rows.extend([_Row(), _Row(name="Acme", status="approved")])

        async def fake_graph():
            return _FakeGraph(_publish_state())

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Reviewer": "rev@example.com",
            },
        )

        assert r.status_code == 409
        assert "no approval token" in r.json()["detail"]

    async def test_media_plan_over_approved_budget_is_refused(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        token = permissions.issue_approval_token(
            str(campaign_id),
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        rows.extend([_Row(), _Row(name="Acme", status="approved")])
        state = _publish_state(token)
        state["brief"]["budget_usd"] = 100.0

        async def fake_graph():
            return _FakeGraph(state)

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)
        webhook = respx.post("https://ads.example.test/publish").mock(
            return_value=httpx.Response(200, json={"external_campaign_id": "g-1"})
        )

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Reviewer": "rev@example.com",
            },
        )

        assert r.status_code == 409
        assert "exceeds the approved campaign budget" in r.json()["detail"]
        assert webhook.call_count == 0

    async def test_missing_reviewer_is_refused(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _enable_writes(monkeypatch)
        campaign_id = uuid4()
        rows.extend([_Row(), _Row(name="Acme", status="approved")])

        async def fake_graph():
            return _FakeGraph(_publish_state("v2.whatever.sig"))

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={"X-AdForge-Tenant": "acme"},
        )

        assert r.status_code == 403
        assert "X-AdForge-Reviewer" in r.json()["detail"]

    async def test_writes_disabled_blocks_publish(self, publish_client, monkeypatch):
        client, _sessions, rows = publish_client
        _prepare_env(
            monkeypatch,
            APP_SECRET="test-secret-not-for-production",
            ADFORGE_ENABLE_LIVE_WRITES="false",
            ADFORGE_PUBLISH_WEBHOOK_URL="https://ads.example.test/publish",
        )
        campaign_id = uuid4()
        token = permissions.issue_approval_token(
            str(campaign_id),
            "publish_campaign",
            "rev@example.com",
            tenant_id="acme",
        )
        rows.extend([_Row(), _Row(name="Acme", status="approved")])

        async def fake_graph():
            return _FakeGraph(_publish_state(token))

        monkeypatch.setattr(reviews, "get_compiled_graph", fake_graph)

        r = await client.post(
            f"/reviews/{campaign_id}/publish",
            headers={
                "X-AdForge-Tenant": "acme",
                "X-AdForge-Reviewer": "rev@example.com",
            },
        )

        assert r.status_code == 409
        assert "live ad writes are disabled" in r.json()["detail"]
