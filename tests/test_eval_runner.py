"""Tests for the agent eval runner's metric guards and live execution path."""
from __future__ import annotations

import sys
from datetime import date

import pytest
from support import VALID_ROI, ScriptedLLM, default_agent_responses

import adforge.agents.kpi_analytics.agent as kpi_agent_module
import adforge.utils.eval_runner as eval_runner
from adforge.agents.kpi_analytics.fetcher import build_snapshot
from adforge.utils.eval_runner import (
    check_kpi_metrics,
    check_roi_metrics,
    main,
    run_eval,
)


def _snapshot(**overrides):
    """A complete, self-consistent KpiSnapshot row."""
    row = {
        "channel": "google",
        "impressions": 10_000,
        "clicks": 500,
        "spend_usd": 1_000.0,
        "conversions": 50,
        "revenue_usd": 4_000.0,
        "ctr": 0.05,
        "cvr": 0.10,
        "cpc": 2.0,
        "cpa": 20.0,
        "cpm": 100.0,
        "roas": 4.0,
    }
    row.update(overrides)
    return row


class TestKpiMetricGuards:
    def test_valid_snapshot_has_no_failures(self):
        assert check_kpi_metrics([_snapshot()]) == []

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [("ctr", 0.5), ("cvr", 0.9), ("roas", 9.0)],
    )
    def test_bad_ratio_is_flagged(self, field, bad_value):
        failures = check_kpi_metrics([_snapshot(**{field: bad_value})])
        assert any(field in failure for failure in failures)

    @pytest.mark.parametrize("field", ["spend_usd", "revenue_usd"])
    def test_negative_money_is_flagged(self, field):
        failures = check_kpi_metrics([_snapshot(**{field: -1.0})])
        assert any("negative money value" in failure for failure in failures)


class TestRoiMetricGuards:
    def test_valid_report_has_no_failures(self):
        assert check_roi_metrics(dict(VALID_ROI)) == []

    @pytest.mark.parametrize(
        ("field", "bad_value", "label"),
        [
            ("blended_roas", 9.0, "blended_roas"),
            ("blended_cpa", 1.0, "blended_cpa"),
            ("net_profit_usd", 1.0, "net_profit"),
        ],
    )
    def test_wrong_derived_metric_is_flagged(self, field, bad_value, label):
        report = dict(VALID_ROI)
        report[field] = bad_value
        failures = check_roi_metrics(report)
        assert any(label in failure for failure in failures)


class TestRunEval:
    @staticmethod
    def _patch_kpi(monkeypatch):
        async def fake_fetch(scope, channel, campaign_id, **kwargs):
            return [
                build_snapshot(
                    channel=channel,
                    campaign_id=campaign_id,
                    campaign_external_id=kwargs.get("external_id"),
                    platform_date=date(2026, 1, 1),
                    impressions=10_000,
                    clicks=500,
                    spend_usd=1_000.0,
                    conversions=50,
                    revenue_usd=4_000.0,
                    source=f"{channel}_ads",
                )
            ]

        async def fake_persist(scope, campaign_id, models):
            return len(models)

        monkeypatch.setattr(kpi_agent_module, "fetch_channel_snapshots", fake_fetch)
        monkeypatch.setattr(kpi_agent_module, "persist_snapshots", fake_persist)

    async def test_eval_with_injected_provider_returns_zero(self, monkeypatch):
        self._patch_kpi(monkeypatch)
        provider = ScriptedLLM(responses=default_agent_responses())
        assert await run_eval(provider=provider, include_graph=False) == 0

    async def test_injected_empty_provider_returns_one(self, monkeypatch):
        self._patch_kpi(monkeypatch)
        provider = ScriptedLLM(default={})
        assert await run_eval(provider=provider, include_graph=False) == 1

    def test_main_skip_graph_exits_zero(self, monkeypatch):
        calls = {}

        async def fake_eval(*, include_graph):
            calls["include_graph"] = include_graph
            return 0

        monkeypatch.setattr(eval_runner, "run_eval", fake_eval)
        monkeypatch.setattr(sys, "argv", ["eval_runner", "--skip-graph"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert calls["include_graph"] is False
