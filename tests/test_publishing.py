"""Unit tests for the approval-gated publishing path.

Every test exercises ``publish_approved_campaign`` directly. The token verifier
and the adapter webhook call are replaced with recording fakes so the money
envelope is validated without any network access or real spend.
"""

from __future__ import annotations

import math

import pytest

from adforge import publishing
from adforge.config import get_settings
from adforge.mcp_servers.ads import writes


def _enable_writes(monkeypatch, **overrides: str) -> None:
    values = {
        "ADFORGE_ENABLE_LIVE_WRITES": "true",
        "ADFORGE_PUBLISH_WEBHOOK_URL": "https://ads.example.test/publish",
        "GUARDRAILS_MAX_CAMPAIGN_BUDGET_USD": "1000000",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _plan(**overrides):
    plan = {
        "channel_allocations": [
            {"channel": "google", "budget_usd": 600.0},
            {"channel": "linkedin", "budget_usd": 400.0},
        ],
        "total_budget_usd": 1000.0,
        "audience_targeting": {"industries": ["SaaS"]},
    }
    plan.update(overrides)
    return plan


def _publish(**overrides):
    values = {
        "campaign_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "acme",
        "reviewer_email": "rev@acme.test",
        "approval_token": "v2.token.sig",
        "campaign_name": "Acme Launch",
        "creative": {"headline": "Ship faster"},
        "media_plan": _plan(),
        "brief": {"budget_usd": 1000.0},
    }
    values.update(overrides)
    return publishing.publish_approved_campaign(**values)


@pytest.fixture
def adapter(monkeypatch):
    calls: list[dict] = []

    async def fake_publish(**kwargs):
        calls.append(kwargs)
        return {"external_campaign_id": f"{kwargs['channel']}-1"}

    monkeypatch.setattr(writes, "publish_campaign", fake_publish)
    return calls


@pytest.fixture
def valid_token(monkeypatch):
    seen: list[dict] = []

    async def fake_verify(token, campaign_id, operation, **kwargs):
        seen.append(
            {
                "token": token,
                "campaign_id": campaign_id,
                "operation": operation,
                **kwargs,
            }
        )
        return {"jti": "one"}

    monkeypatch.setattr(publishing, "verify_and_consume_approval", fake_verify)
    return seen


class TestWriteGuards:
    async def test_writes_disabled_is_refused(self, monkeypatch, adapter):
        _enable_writes(monkeypatch, ADFORGE_ENABLE_LIVE_WRITES="false")

        with pytest.raises(publishing.PublishError, match="writes are disabled"):
            await _publish()

        assert adapter == []

    async def test_missing_adapter_webhook_is_refused(self, monkeypatch, adapter):
        _enable_writes(monkeypatch, ADFORGE_PUBLISH_WEBHOOK_URL="")

        with pytest.raises(publishing.PublishError, match="WEBHOOK_URL"):
            await _publish()

        assert adapter == []

    async def test_invalid_token_is_refused_before_any_spend(self, monkeypatch, adapter):
        _enable_writes(monkeypatch)

        async def reject(*args, **kwargs):
            return None

        monkeypatch.setattr(publishing, "verify_and_consume_approval", reject)

        with pytest.raises(publishing.PublishError, match="approval token"):
            await _publish()

        assert adapter == []


class TestMoneyEnvelope:
    @pytest.mark.parametrize("total", [None, "abc", 0, -1.0, math.inf, math.nan])
    async def test_invalid_plan_total_is_refused(self, monkeypatch, adapter, valid_token, total):
        _enable_writes(monkeypatch)

        with pytest.raises(publishing.PublishError, match="total_budget_usd"):
            await _publish(media_plan=_plan(total_budget_usd=total))

        assert adapter == []

    async def test_plan_total_over_configured_cap_is_refused(
        self, monkeypatch, adapter, valid_token
    ):
        _enable_writes(monkeypatch, GUARDRAILS_MAX_CAMPAIGN_BUDGET_USD="500")

        with pytest.raises(publishing.PublishError, match="configured cap"):
            await _publish(media_plan=_plan(total_budget_usd=1000.0))

        assert adapter == []

    async def test_plan_total_over_approved_brief_is_refused(
        self, monkeypatch, adapter, valid_token
    ):
        _enable_writes(monkeypatch)

        with pytest.raises(publishing.PublishError, match="approved campaign budget"):
            await _publish(brief={"budget_usd": 100.0})

        assert adapter == []

    @pytest.mark.parametrize("brief", [None, {}])
    async def test_missing_brief_budget_still_uses_cap(
        self, monkeypatch, adapter, valid_token, brief
    ):
        _enable_writes(monkeypatch)

        results = await _publish(brief=brief)

        assert len(results) == 2
        assert len(adapter) == 2

    async def test_allocations_over_plan_total_are_refused(self, monkeypatch, adapter, valid_token):
        _enable_writes(monkeypatch)
        plan = _plan(
            channel_allocations=[
                {"channel": "google", "budget_usd": 900.0},
                {"channel": "linkedin", "budget_usd": 200.0},
            ]
        )

        with pytest.raises(publishing.PublishError, match="exceeds the media plan budget"):
            await _publish(media_plan=plan)

        assert adapter == []

    @pytest.mark.parametrize("bad_budget", [None, "abc", 0, -5.0, math.inf, math.nan])
    async def test_invalid_channel_budget_is_refused(
        self, monkeypatch, adapter, valid_token, bad_budget
    ):
        _enable_writes(monkeypatch)
        plan = _plan(
            channel_allocations=[
                {"channel": "google", "budget_usd": bad_budget},
            ],
            total_budget_usd=1000.0,
        )

        with pytest.raises(publishing.PublishError, match="budget for channel"):
            await _publish(media_plan=plan)

        assert adapter == []

    async def test_no_allocations_is_refused(self, monkeypatch, adapter, valid_token):
        _enable_writes(monkeypatch)

        with pytest.raises(publishing.PublishError, match="no channel allocations"):
            await _publish(media_plan=_plan(channel_allocations=[]))

        assert adapter == []

    async def test_blank_channel_is_refused(self, monkeypatch, adapter, valid_token):
        _enable_writes(monkeypatch)
        plan = _plan(channel_allocations=[{"channel": "   ", "budget_usd": 100.0}])

        with pytest.raises(publishing.PublishError, match="must name a channel"):
            await _publish(media_plan=plan)

        assert adapter == []

    @pytest.mark.parametrize("channels", [("google", "Google"), ("meta", "META")])
    async def test_duplicate_channels_are_refused(
        self, monkeypatch, adapter, valid_token, channels
    ):
        _enable_writes(monkeypatch)
        plan = _plan(
            channel_allocations=[
                {"channel": channels[0], "budget_usd": 500.0},
                {"channel": channels[1], "budget_usd": 500.0},
            ]
        )

        with pytest.raises(publishing.PublishError, match="duplicate channel"):
            await _publish(media_plan=plan)

        assert adapter == []

    async def test_allocations_may_be_under_plan_total(self, monkeypatch, adapter, valid_token):
        _enable_writes(monkeypatch)
        plan = _plan(
            channel_allocations=[{"channel": "google", "budget_usd": 250.0}],
            total_budget_usd=1000.0,
        )

        results = await _publish(media_plan=plan)

        assert results == [{"external_campaign_id": "google-1"}]
        assert adapter[0]["budget_usd"] == 250.0


class TestPublishExecution:
    async def test_token_is_verified_for_the_exact_campaign_and_tenant(
        self, monkeypatch, adapter, valid_token
    ):
        _enable_writes(monkeypatch)

        await _publish()

        assert valid_token == [
            {
                "token": "v2.token.sig",
                "campaign_id": "11111111-1111-1111-1111-111111111111",
                "operation": "publish_campaign",
                "tenant_id": "acme",
                "reviewer_email": "rev@acme.test",
            }
        ]

    async def test_every_allocation_is_published_with_audience(
        self, monkeypatch, adapter, valid_token
    ):
        _enable_writes(monkeypatch)

        results = await _publish()

        assert results == [
            {"external_campaign_id": "google-1"},
            {"external_campaign_id": "linkedin-1"},
        ]
        assert [call["channel"] for call in adapter] == ["google", "linkedin"]
        assert all(call["audience"] == {"industries": ["SaaS"]} for call in adapter)

    async def test_adapter_failure_reports_partial_progress(self, monkeypatch, valid_token):
        _enable_writes(monkeypatch)
        calls: list[str] = []

        async def fail_second(**kwargs):
            calls.append(kwargs["channel"])
            if kwargs["channel"] == "linkedin":
                raise writes.WriteGatewayError("webhook rejected")
            return {"external_campaign_id": "google-1"}

        monkeypatch.setattr(writes, "publish_campaign", fail_second)

        with pytest.raises(publishing.PublishError, match="after 1 successful publish"):
            await _publish()

        assert calls == ["google", "linkedin"]
