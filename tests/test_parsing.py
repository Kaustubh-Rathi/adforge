"""Unit tests for shared JSON extraction + contract validation helpers."""
from __future__ import annotations

import pytest

from adforge.agents.parsing import extract_json, strip_code_fence, validate_payload
from adforge.guardrails.contracts import ResearchFindings
from adforge.guardrails.exceptions import ContractValidationError

VALID = {
    "market_summary": "A growing market with strong demand.",
    "top_keywords": ["crm", "sales"],
    "competitor_creatives": [],
    "audience_persona": {"title": "Sales Manager"},
    "trends": [],
    "sources": ["https://example.com"],
}


class TestStripCodeFence:
    def test_plain_text_is_returned_trimmed(self):
        assert strip_code_fence("  hello  ") == "hello"

    def test_json_fence_is_removed(self):
        assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_fence_is_removed(self):
        assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_unterminated_fence_drops_opening_line(self):
        assert strip_code_fence('```json\n{"a": 1}') == '{"a": 1}'

    def test_single_line_fence_without_newline(self):
        assert strip_code_fence("```json") == ""


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_object_with_leading_and_trailing_prose(self):
        content = 'Here you go:\n{"a": 1, "b": [2, 3]}\nHope that helps!'
        assert extract_json(content) == {"a": 1, "b": [2, 3]}

    def test_fenced_object(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_inner_object_wins_when_outer_text_is_not_json(self):
        assert extract_json('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}

    def test_non_object_json_is_rejected(self):
        with pytest.raises(ContractValidationError) as exc:
            extract_json("[1, 2, 3]")
        assert exc.value.agent_name == "response"
        assert exc.value.errors[0]["type"] == "json_decode"

    def test_garbage_is_rejected(self):
        with pytest.raises(ContractValidationError):
            extract_json("definitely not json")

    def test_empty_string_is_rejected(self):
        with pytest.raises(ContractValidationError):
            extract_json("")


class TestValidatePayload:
    def test_valid_payload_is_dumped_to_json_compatible_dict(self):
        result = validate_payload(ResearchFindings, VALID, "research")
        assert result["market_summary"] == VALID["market_summary"]
        assert result["sources"] == VALID["sources"]

    def test_invalid_payload_raises_typed_error(self):
        with pytest.raises(ContractValidationError) as exc:
            validate_payload(ResearchFindings, {"market_summary": ""}, "research")
        assert exc.value.agent_name == "research"
        assert exc.value.errors
        assert "market_summary" in str(exc.value)

    def test_extra_fields_are_rejected_by_contract(self):
        payload = {**VALID, "unexpected": True}
        with pytest.raises(ContractValidationError):
            validate_payload(ResearchFindings, payload, "research")
