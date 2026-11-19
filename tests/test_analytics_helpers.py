"""Unit tests for the JSON boundary helpers used by the analytics MCP server.

MCP callers send JSON, so dates/timestamps arrive as strings while asyncpg
requires real ``date``/``datetime`` objects. These pin that coercion contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from adforge.mcp_servers.analytics import (
    _coerce_date,
    _coerce_datetime,
    _jsonable_row,
)


class TestCoerceDate:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (date(2026, 1, 5), date(2026, 1, 5)),
            (datetime(2026, 1, 6, 23, 59), date(2026, 1, 6)),
            ("2026-01-07", date(2026, 1, 7)),
            ("2026-01-08T09:10:11Z", date(2026, 1, 8)),
            ("20260109", date(2026, 1, 9)),
            ("  2026-01-10  ", date(2026, 1, 10)),
        ],
    )
    def test_supported_shapes(self, value, expected):
        assert _coerce_date(value) == expected

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            _coerce_date("not-a-date")


class TestCoerceDatetime:
    def test_datetime_passes_through(self):
        value = datetime(2026, 1, 5, tzinfo=UTC)
        assert _coerce_datetime(value) is value

    def test_z_suffix_is_utc_aware(self):
        parsed = _coerce_datetime("2026-01-05T10:00:00Z")
        assert parsed == datetime(2026, 1, 5, 10, tzinfo=UTC)
        assert parsed.tzinfo is not None

    def test_naive_datetime_is_assumed_utc(self):
        parsed = _coerce_datetime("2026-01-05T10:00:00")
        assert parsed.tzinfo is UTC

    def test_date_only_falls_back_to_midnight(self):
        parsed = _coerce_datetime("2026-01-05")
        assert parsed == datetime(2026, 1, 5, tzinfo=UTC)


class TestJsonableRow:
    def test_converts_dates_and_uuids(self):
        row_id = uuid4()
        clean = _jsonable_row(
            {
                "id": row_id,
                "platform_date": date(2026, 1, 5),
                "captured_at": datetime(2026, 1, 5, 9, tzinfo=UTC),
                "channel": "google",
                "spend_usd": 12.5,
                "is_mock": False,
                "external": None,
            }
        )
        assert clean["id"] == str(row_id)
        assert clean["platform_date"] == "2026-01-05"
        assert clean["captured_at"] == "2026-01-05T09:00:00+00:00"
        assert clean["channel"] == "google"
        assert clean["spend_usd"] == 12.5
        assert clean["is_mock"] is False
        assert clean["external"] is None
