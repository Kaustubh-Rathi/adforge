"""Extra guardrail tests: approval tokens, write decorators, rate limiting,
audit log, policy enforcement, contract validation and API dependencies.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from adforge.api import deps
from adforge.config import get_settings
from adforge.guardrails import audit_log as audit_module
from adforge.guardrails import rate_limiter as rate_module
from adforge.guardrails.contracts import CampaignBrief, KpiSnapshot, MediaPlan
from adforge.guardrails.data_isolation import TenantScope
from adforge.guardrails.exceptions import PolicyViolation, RateLimitExceeded
from adforge.guardrails.policy import PolicyEnforcer, PolicyRule
from adforge.mcp_servers import permissions
from adforge.storage import session as session_module


def _prepare_env(monkeypatch, **overrides) -> None:
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def _tamper(token: str) -> str:
    signed, _, reviewer = token.partition("#")
    parts = signed.split(":")
    signature = parts[-1]
    replacement = "0" if signature[-1] != "0" else "1"
    parts[-1] = signature[:-1] + replacement
    tampered = ":".join(parts)
    return f"{tampered}#{reviewer}" if reviewer else tampered


# --------------------------------------------------------------------------- #
# Approval tokens
# --------------------------------------------------------------------------- #


class TestApprovalTokens:
    def test_valid_token_verifies(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(campaign_id, "publish_campaign", "rev@example.com")
        assert permissions._verify_approval_token(token, campaign_id, "publish_campaign") is True

    def test_wrong_campaign_rejected(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(campaign_id, "publish_campaign", "rev@example.com")
        assert permissions._verify_approval_token(token, str(uuid4()), "publish_campaign") is False

    def test_wrong_operation_rejected(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(campaign_id, "publish_campaign", "rev@example.com")
        assert permissions._verify_approval_token(token, campaign_id, "pause_campaign") is False

    def test_expired_token_rejected(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id, "publish_campaign", "rev@example.com", ttl_seconds=-1
        )
        assert permissions._verify_approval_token(token, campaign_id, "publish_campaign") is False

    def test_tampered_signature_rejected(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(campaign_id, "publish_campaign", "rev@example.com")
        assert (
            permissions._verify_approval_token(_tamper(token), campaign_id, "publish_campaign")
            is False
        )

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "not-a-token",
            "a:b:c",
            "a:b:not-an-int:deadbeef",
            "a:b:9999999999:deadbeef",
        ],
    )
    def test_junk_tokens_rejected(self, junk):
        assert permissions._verify_approval_token(junk, "a", "b") is False

    def test_reviewer_is_signed_into_token(self):
        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id, "publish_campaign", "reviewer+audit@example.com"
        )
        claims = permissions._verify_approval_claims(
            token, campaign_id, "publish_campaign"
        )
        assert claims is not None
        assert claims["reviewer_email"] == "reviewer+audit@example.com"
        assert token.split(".")[0] == "v2"

    def test_missing_app_secret_rejects_all_and_cannot_issue(self, monkeypatch):
        _prepare_env(monkeypatch, APP_SECRET="")
        assert get_settings().app_secret == ""

        assert permissions._verify_approval_token("a:b:9999999999:deadbeef", "a", "b") is False
        with pytest.raises(RuntimeError, match="APP_SECRET"):
            permissions.issue_approval_token("a", "b", "rev@example.com")


# --------------------------------------------------------------------------- #
# write_operation decorator
# --------------------------------------------------------------------------- #


class TestWriteOperation:
    @pytest.mark.parametrize("headers", [None, {}])
    async def test_missing_tenant_is_rejected(self, headers):
        @permissions.write_operation(requires_approval=True)
        async def do_write(headers=None):
            return json.dumps({"executed": True})

        data = json.loads(await do_write(headers=headers))

        assert data["error"] == "missing tenant header"

    async def test_approval_required_without_token(self):
        @permissions.write_operation(requires_approval=True)
        async def do_write(headers=None):
            return json.dumps({"executed": True})

        campaign_id = str(uuid4())
        data = json.loads(
            await do_write(
                headers={
                    "X-AdForge-Tenant": "acme",
                    "X-AdForge-Campaign": campaign_id,
                }
            )
        )

        assert data["error"] == "approval_required"
        assert data["operation"] == "do_write"
        assert data["campaign_id"] == campaign_id

    async def test_valid_token_executes(self):
        @permissions.write_operation(requires_approval=True)
        async def do_write(headers=None):
            return json.dumps({"executed": True})

        campaign_id = str(uuid4())
        token = permissions.issue_approval_token(
            campaign_id, "do_write", "rev@example.com", tenant_id="acme"
        )
        data = json.loads(
            await do_write(
                headers={
                    "X-AdForge-Tenant": "acme",
                    "X-AdForge-Campaign": campaign_id,
                    "X-AdForge-Approval-Token": token,
                }
            )
        )

        assert data == {"executed": True}

    async def test_requires_approval_false_only_needs_tenant(self):
        @permissions.write_operation(requires_approval=False)
        async def internal_write(headers=None):
            return json.dumps({"executed": True})

        data = json.loads(await internal_write(headers={"X-AdForge-Tenant": "acme"}))

        assert data == {"executed": True}


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


class TestRateLimiter:
    async def test_inactive_guardrails_noop_without_redis(self, monkeypatch):
        _prepare_env(monkeypatch, APP_ENV="test", APP_DISABLE_GUARDRAILS="true")
        rate_module._limiters.clear()
        touched: list[bool] = []

        async def boom():
            touched.append(True)
            raise AssertionError("Redis must not be touched when guardrails are off")

        monkeypatch.setattr(rate_module, "get_redis", boom)

        await rate_module.RateLimiter(max_per_minute=1).acquire(
            "google", "tenant_off", "campaign_off"
        )

        assert touched == []
        assert get_settings().guardrails_active is False

    async def test_per_minute_limit_allows_two_then_raises(self, monkeypatch):
        _prepare_env(monkeypatch, APP_ENV="test", APP_DISABLE_GUARDRAILS="false")
        limiter = rate_module.RateLimiter(max_per_minute=2)
        tenant = f"tenant_{uuid4()}"

        await limiter.acquire("google", tenant, "campaign")
        await limiter.acquire("google", tenant, "campaign")

        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.acquire("google", tenant, "campaign")

        exc = exc_info.value
        assert exc.limit == 2
        assert exc.window_seconds == 60
        assert exc.scope == f"google:{tenant}"

    async def test_llm_per_campaign_limit_raises_on_second(self, monkeypatch):
        _prepare_env(monkeypatch, APP_ENV="test", APP_DISABLE_GUARDRAILS="false")
        limiter = rate_module.RateLimiter(max_per_minute=100, max_per_campaign=1)
        tenant = f"tenant_{uuid4()}"
        campaign = str(uuid4())

        await limiter.acquire("llm", tenant, campaign)

        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.acquire("llm", tenant, campaign)

        exc = exc_info.value
        assert exc.scope == f"llm:campaign:{campaign}"
        assert exc.limit == 1
        assert exc.window_seconds == 86400

    async def test_redis_failure_fails_open(self, monkeypatch, caplog):
        _prepare_env(monkeypatch, APP_ENV="test", APP_DISABLE_GUARDRAILS="false")

        async def down():
            raise RuntimeError("redis is down")

        monkeypatch.setattr(rate_module, "get_redis", down)
        caplog.set_level(logging.WARNING, logger="adforge.ratelimit")

        # Must not raise: a Redis outage should not take the platform down.
        await rate_module.RateLimiter(max_per_minute=1).acquire(
            "google", "tenant_down", "campaign_down"
        )

        assert "unavailable" in caplog.text

    def test_get_rate_limiter_scope_limits_and_caching(self, monkeypatch):
        _prepare_env(
            monkeypatch,
            APP_ENV="test",
            APP_DISABLE_GUARDRAILS="false",
            GUARDRAILS_MAX_LLM_CALLS_PER_CAMPAIGN="17",
            GUARDRAILS_MAX_API_CALLS_PER_MIN="23",
        )
        rate_module._limiters.clear()

        llm_limiter = rate_module.get_rate_limiter("llm")
        platform_limiter = rate_module.get_rate_limiter("google")

        assert llm_limiter.max_per_minute == 23
        assert llm_limiter.max_per_campaign == 17
        assert platform_limiter.max_per_minute == 23
        assert platform_limiter.max_per_campaign is None

        assert rate_module.get_rate_limiter("llm") is llm_limiter
        assert rate_module.get_rate_limiter("google") is platform_limiter


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


class AuditFakeSession:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict]] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None) -> None:
        self.executions.append((str(statement), params))

    async def commit(self) -> None:
        self.commits += 1


class AuditFakeFactory:
    def __init__(self, session: AuditFakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


class TestAuditLog:
    async def test_failed_flush_rebuffers_event(self, caplog):
        log = audit_module.AuditLog()
        event = audit_module.AuditEvent(
            event_type=audit_module.AuditEventType.AGENT_START,
            tenant_id="acme",
        )
        caplog.set_level(logging.WARNING, logger="adforge.audit")

        # The unit fixture's session factory raises (database disabled).
        await log.record(event)

        assert log._buffer == [event]
        assert "re-buffered" in caplog.text

    async def test_flush_with_empty_buffer_is_noop(self):
        log = audit_module.AuditLog()

        await log.flush()

        assert log._buffer == []

    async def test_record_with_fake_session_factory_inserts_and_commits(self, monkeypatch):
        session = AuditFakeSession()
        monkeypatch.setattr(
            session_module,
            "get_session_factory",
            lambda: AuditFakeFactory(session),
        )
        log = audit_module.AuditLog()
        campaign_id = uuid4()
        event = audit_module.AuditEvent(
            event_type=audit_module.AuditEventType.LLM_CALL,
            tenant_id="acme",
            campaign_id=campaign_id,
            agent_name="research",
            payload={"model": "glm-4.6", "tokens": 42},
        )

        await log.record(event)

        assert len(session.executions) == 1
        statement, params = session.executions[0]
        assert "INSERT INTO audit_events" in statement
        assert params["eid"] == str(event.event_id)
        assert params["et"] == "llm.call"
        assert params["tid"] == "acme"
        assert params["cid"] == str(campaign_id)
        assert params["an"] == "research"
        assert json.loads(params["p"]) == {"model": "glm-4.6", "tokens": 42}
        assert session.commits == 1
        assert log._buffer == []


# --------------------------------------------------------------------------- #
# Policy enforcer
# --------------------------------------------------------------------------- #


class TestPolicyEnforcerExtra:
    def setup_method(self):
        self.enforcer = PolicyEnforcer.default()

    def test_default_loads_bundled_yaml(self):
        rule_ids = {rule.rule_id for rule in self.enforcer.rules}
        assert len(self.enforcer.rules) >= 9
        assert {"BLOCK_TOBACCO", "DISCLOSE_FINANCIAL", "WARN_MIRACLE"} <= rule_ids

    def test_clean_creative_approved_low_risk(self):
        verdict = self.enforcer.evaluate("Build better software with Acme SaaS. Start free.")
        assert verdict.approved is True
        assert verdict.risk_level == "low"
        assert verdict.flagged_issues == []

    def test_tier1_tobacco_blocked_critical(self):
        verdict = self.enforcer.evaluate("Buy premium tobacco products today!")
        assert verdict.approved is False
        assert verdict.risk_level == "critical"
        assert "BLOCK_TOBACCO" in verdict.blocked_terms

    def test_tier2_financial_requires_disclosure_but_approves(self):
        verdict = self.enforcer.evaluate("Guaranteed return on your crypto investment")
        assert verdict.approved is True
        assert verdict.risk_level == "high"
        assert verdict.required_disclosures
        assert any("Investing involves risk" in d for d in verdict.required_disclosures)
        assert any("DISCLOSE_FINANCIAL" in issue for issue in verdict.flagged_issues)

    def test_tier3_health_warning_is_medium_but_approved(self):
        verdict = self.enforcer.evaluate("Our miracle formula delivers instant results")
        assert verdict.approved is True
        assert verdict.risk_level == "medium"
        assert any("WARN_MIRACLE" in issue for issue in verdict.flagged_issues)

    def test_sensitive_audience_targeting_blocked(self):
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={"race": "asian"},
        )
        assert verdict.approved is False
        assert verdict.risk_level == "critical"
        assert "TARGETING_RACE" in verdict.blocked_terms

    def test_sensitive_attributes_in_exclusion_lists_are_not_targeting(self):
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "interests": ["sales software"],
                "exclusions": ["race", "ethnicity", "religion", "sexual_orientation"],
                "sensitive_attributes": {"excluded": ["religion"]},
            },
        )
        assert verdict.approved is True
        assert verdict.risk_level == "low"
        assert verdict.blocked_terms == []

    def test_positive_targeting_still_blocks_alongside_exclusions(self):
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "interests": ["religion"],
                "exclusions": ["race"],
            },
        )
        assert verdict.approved is False
        assert "TARGETING_RELIGION" in verdict.blocked_terms
        assert "TARGETING_RACE" not in verdict.blocked_terms

    def test_nested_exclusion_key_skips_entire_subtree(self):
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "targeting": {"job_titles": ["sales manager"]},
                "do_not_target": {"attributes": ["ethnicity"]},
            },
        )
        assert verdict.approved is True

    def test_descriptive_policy_fields_are_not_targeting(self):
        # Shape observed from a real MediaPlan: prose fields explain that
        # sensitive attributes are deliberately not targeted. Scanning them
        # must not hard-block the campaign.
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "google": {
                    "type": "search_intent",
                    "geography": ["United States"],
                    "constraint_notes": (
                        "No comparison-brand bidding; no targeting on race, "
                        "ethnicity, or religion."
                    ),
                },
                "linkedin": {
                    "job_titles": ["Sales Manager", "VP Sales"],
                    "company_size": ["11-50", "51-200"],
                    "exclusions": ["current customers", "students"],
                    "sensitive_attribute_policy": (
                        "No targeting or exclusion on race, ethnicity, religion, "
                        "sexual orientation, health conditions, or income status."
                    ),
                },
            },
        )
        assert verdict.approved is True
        assert verdict.risk_level == "low"
        assert verdict.blocked_terms == []

    def test_sensitive_attribute_check_fields_are_not_targeting(self):
        # Real MediaPlans carry a per-channel self-assessment such as
        # "sensitive_attribute_check": "pass - no age, gender, ethnicity,
        # religion...". That is commentary, not targeting; scanning it caused
        # a live HITL run to auto-block with TARGETING_RELIGION/ETHNICITY.
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "google": {
                    "targeting_type": "search_intent",
                    "geo": ["United States"],
                    "sensitive_attribute_check": (
                        "pass - no demographic, health, financial-status or "
                        "identity targeting set"
                    ),
                },
                "linkedin": {
                    "job_titles": ["Sales Manager"],
                    "sensitive_attribute_check": (
                        "pass - no age, gender, ethnicity, religion, sexual "
                        "orientation, health or financial-status facets set"
                    ),
                },
            },
        )
        assert verdict.approved is True
        assert verdict.risk_level == "low"
        assert verdict.blocked_terms == []

    def test_positive_targeting_blocks_despite_descriptive_fields(self):
        verdict = self.enforcer.evaluate(
            "Great product for everyone",
            audience_targeting={
                "linkedin": {
                    "interests": ["religion"],
                    "sensitive_attribute_policy": "No targeting on race or ethnicity.",
                },
            },
        )
        assert verdict.approved is False
        assert "TARGETING_RELIGION" in verdict.blocked_terms
        assert "TARGETING_RACE" not in verdict.blocked_terms
        assert "TARGETING_ETHNICITY" not in verdict.blocked_terms

    def test_invalid_regex_falls_back_to_substring(self):
        enforcer = PolicyEnforcer(
            rules=[
                PolicyRule(
                    rule_id="BAD_REGEX",
                    tier=1,
                    pattern="[unclosed",
                    description="Deliberately invalid regex",
                )
            ]
        )
        verdict = enforcer.evaluate("This copy contains [unclosed bracket text")
        assert verdict.approved is False
        assert "BAD_REGEX" in verdict.blocked_terms

    def test_assert_approved_raises_for_blocked(self):
        with pytest.raises(PolicyViolation) as exc_info:
            self.enforcer.assert_approved("Buy our cigarettes online!")
        assert "BLOCK_TOBACCO" in exc_info.value.rule_id
        assert "critical" in exc_info.value.detail

    def test_assert_approved_returns_verdict_for_clean(self):
        verdict = self.enforcer.assert_approved("A perfectly clean ad about software.")
        assert verdict.approved is True
        assert verdict.risk_level == "low"

    def test_from_yaml_loads_custom_rules(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "rule_id": "BLOCK_CUSTOM",
                            "tier": 1,
                            "pattern": "forbidden phrase",
                            "description": "Custom test rule",
                        },
                        {
                            "rule_id": "DISCLOSE_CUSTOM",
                            "tier": 2,
                            "pattern": "custom claim",
                            "description": "Custom disclosure rule",
                            "required_disclosure": "Custom disclosure text.",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        enforcer = PolicyEnforcer.from_yaml(path)

        assert [r.rule_id for r in enforcer.rules] == [
            "BLOCK_CUSTOM",
            "DISCLOSE_CUSTOM",
        ]
        assert enforcer.evaluate("This has a forbidden phrase").approved is False
        disclosure_verdict = enforcer.evaluate("This has a custom claim")
        assert disclosure_verdict.approved is True
        assert disclosure_verdict.required_disclosures == ["Custom disclosure text."]


# --------------------------------------------------------------------------- #
# Contract validation
# --------------------------------------------------------------------------- #


def _valid_media_plan(**overrides) -> MediaPlan:
    values = {
        "channel_allocations": [{"channel": "google", "budget_usd": 1000.0}],
        "total_budget_usd": 1000.0,
        "bidding_strategy": "max_conversions",
        "audience_targeting": {"age": "25-44"},
        "expected_reach": 10_000,
        "expected_impressions": 40_000,
        "flight_dates": {"start": "2026-01-01", "end": "2026-02-01"},
    }
    values.update(overrides)
    return MediaPlan(**values)


class TestContractValidation:
    def test_media_plan_rejects_empty_allocations(self):
        with pytest.raises(ValidationError):
            _valid_media_plan(channel_allocations=[])

    def test_media_plan_rejects_missing_channel(self):
        with pytest.raises(ValidationError):
            _valid_media_plan(channel_allocations=[{"budget_usd": 1000.0}])

    def test_media_plan_rejects_missing_budget(self):
        with pytest.raises(ValidationError):
            _valid_media_plan(channel_allocations=[{"channel": "google"}])

    @pytest.mark.parametrize("budget", [0, -1.0])
    def test_media_plan_rejects_non_positive_budget(self, budget):
        with pytest.raises(ValidationError):
            _valid_media_plan(channel_allocations=[{"channel": "google", "budget_usd": budget}])

    @pytest.mark.parametrize("budget", [float("inf"), float("nan")])
    def test_media_plan_rejects_non_finite_budget(self, budget):
        with pytest.raises(ValidationError):
            _valid_media_plan(
                channel_allocations=[{"channel": "google", "budget_usd": budget}]
            )

    def test_media_plan_rejects_duplicate_channels(self):
        with pytest.raises(ValidationError):
            _valid_media_plan(
                channel_allocations=[
                    {"channel": "google", "budget_usd": 500.0},
                    {"channel": "Google", "budget_usd": 500.0},
                ]
            )

    def test_media_plan_rejects_allocations_over_total(self):
        with pytest.raises(ValidationError):
            _valid_media_plan(
                channel_allocations=[{"channel": "google", "budget_usd": 1001.0}]
            )

    def test_campaign_brief_rejects_duplicate_channels(self, sample_brief_dict):
        brief = dict(sample_brief_dict)
        brief["channels_hint"] = ["google", "google"]
        with pytest.raises(ValidationError):
            CampaignBrief(**brief)

    def test_campaign_brief_rejects_zero_budget(self, sample_brief_dict):
        brief = dict(sample_brief_dict)
        brief["budget_usd"] = 0
        with pytest.raises(ValidationError):
            CampaignBrief(**brief)

    def test_kpi_snapshot_rejects_ctr_above_one(self):
        with pytest.raises(ValidationError):
            KpiSnapshot(
                channel="google",
                impressions=1000,
                clicks=100,
                spend_usd=10.0,
                conversions=5,
                revenue_usd=50.0,
                ctr=1.01,
                cvr=0.1,
                cpc=0.1,
                cpa=2.0,
                cpm=10.0,
                roas=5.0,
            )


# --------------------------------------------------------------------------- #
# API dependencies
# --------------------------------------------------------------------------- #


class TestApiDeps:
    async def test_missing_tenant_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            await deps.get_tenant_scope("", "")
        assert exc_info.value.status_code == 400
        assert "X-AdForge-Tenant" in exc_info.value.detail

    async def test_invalid_campaign_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            await deps.get_tenant_scope("acme", "not-a-uuid")
        assert exc_info.value.status_code == 400
        assert "X-AdForge-Campaign" in exc_info.value.detail

    async def test_valid_headers_return_tenant_scope(self):
        campaign_id = uuid4()
        scope = await deps.get_tenant_scope("acme", str(campaign_id))
        assert isinstance(scope, TenantScope)
        assert scope.tenant_id == "acme"
        assert scope.campaign_id == campaign_id

    async def test_missing_campaign_header_uses_zero_uuid(self):
        scope = await deps.get_tenant_scope("acme", "")
        assert scope.campaign_id == UUID(int=0)
