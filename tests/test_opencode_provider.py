"""Unit tests for the opencode.ai-aware OpenAI-compatible LLM provider.

The live endpoint is exercised separately in ``test_live_services.py``. These
tests pin the wire format (headers, <env> block, read-gate tool), the SSE/JSON
parsers, and both transports without touching the network.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import adforge.llm.opencode_provider as oc
from adforge.guardrails.exceptions import (
    GuardrailError,
    LLMQuotaExceededError,
    LLMTransportError,
)
from adforge.llm.opencode_provider import (
    OpencodeProvider,
    generate_session_id,
)

BASE = "https://opencode.ai/inference/openai/v1"
OTHER = "https://llm.example.test/v1"


def _provider(base_url: str = BASE) -> OpencodeProvider:
    return OpencodeProvider(base_url=base_url, model="test-model", api_key="public", timeout=5)


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def _chunk(content: str, finish: str | None = None) -> dict:
    return {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}


class TestConfiguration:
    def test_missing_configuration_raises_guardrail_error(self):
        with pytest.raises(GuardrailError):
            OpencodeProvider(base_url="", model="", api_key="")

    def test_invalid_scheme_raises(self):
        with pytest.raises(GuardrailError, match="absolute http"):
            OpencodeProvider(base_url="ftp://opencode.ai/v1", model="m", api_key="k")

    def test_is_opencode_detection(self):
        assert _provider().is_opencode is True
        assert _provider("https://api.opencode.ai/v1").is_opencode is True
        assert _provider(OTHER).is_opencode is False

    def test_chat_completions_url(self):
        assert _provider().chat_completions_url == f"{BASE}/chat/completions"
        explicit = _provider(f"{BASE}/chat/completions")
        assert explicit.chat_completions_url == f"{BASE}/chat/completions"


class TestSessionIds:
    def test_format_and_length(self):
        sid = generate_session_id(1_700_000_000_000)
        assert sid.startswith("ses_")
        assert len(sid) == 30
        hex_part = sid[4:16]
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_ids_are_unique_and_advance(self):
        ids = {generate_session_id(1_700_000_000_000) for _ in range(5)}
        assert len(ids) == 5

    def test_negative_timestamp_rejected(self):
        with pytest.raises(ValueError):
            generate_session_id(-1)

    def test_counter_rollover_bumps_millisecond(self, monkeypatch):
        monkeypatch.setattr(oc, "_last_session_ms", 1000)
        monkeypatch.setattr(oc, "_session_counter", oc._SESSION_COUNTER_LIMIT - 1)
        generate_session_id(1000)
        assert oc._last_session_ms == 1001
        assert oc._session_counter == 1


class TestWireFormat:
    def test_non_opencode_headers_are_plain(self):
        headers = _provider(OTHER)._headers(None)
        assert headers["Authorization"] == "Bearer public"
        assert "User-Agent" not in headers
        assert "x-opencode-session" not in headers

    def test_opencode_headers_include_session_and_trace(self):
        headers = _provider()._headers("ses_abc")
        assert headers["x-opencode-client"] == "cli"
        assert headers["x-opencode-session"] == "ses_abc"
        assert headers["x-opencode-session-id"] == "ses_abc"
        assert headers["x-session-affinity"] == "ses_abc"
        assert headers["x-session-id"] == "ses_abc"
        trace_id, span_id, sampled, parent = headers["b3"].split("-")
        assert len(trace_id) == 32 and len(span_id) == 16 and len(parent) == 16
        assert sampled == "1"
        assert headers["traceparent"].startswith(f"00-{trace_id}-{span_id}-01")

    def test_opencode_without_session_omits_session_headers(self):
        headers = _provider()._headers(None)
        assert "x-opencode-session" not in headers

    def test_env_block_appended_to_existing_system_message(self):
        messages = [
            {"role": "system", "content": "You are AdForge."},
            {"role": "user", "content": "hi"},
        ]
        prepared = _provider()._prepare_messages(messages)
        assert prepared[0]["content"].startswith("You are AdForge.")
        assert "<env>" in prepared[0]["content"]
        assert "Working directory: /workspace" in prepared[0]["content"]

    def test_existing_env_block_is_not_duplicated(self):
        content = "You are AdForge.\n\n<env>\nPlatform: linux\n</env>"
        prepared = _provider()._prepare_messages([{"role": "system", "content": content}])
        assert prepared[0]["content"] == content

    def test_missing_system_message_gets_one(self):
        prepared = _provider()._prepare_messages([{"role": "user", "content": "hi"}])
        assert prepared[0]["role"] == "system"
        assert "<env>" in prepared[0]["content"]
        assert prepared[1]["content"] == "hi"

    def test_non_opencode_messages_are_untouched(self):
        messages = [{"role": "user", "content": "hi"}]
        assert _provider(OTHER)._prepare_messages(messages) == messages

    def test_payload_uses_env_token_and_stream_options(self, monkeypatch):
        monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
        monkeypatch.setenv("LLM_MAX_TOKENS", "999")
        from adforge.config import get_settings

        get_settings.cache_clear()
        payload = _provider()._build_payload([{"role": "system", "content": "sys"}], None, None)
        assert payload["model"] == "test-model"
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 999
        assert payload["tools"][0]["function"]["name"] == "read"

    def test_opencode_sends_read_gate_plus_caller_tools(self):
        payload = _provider()._build_payload(
            [{"role": "user", "content": "hi"}],
            0.1,
            128,
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
        assert [t["function"]["name"] for t in payload["tools"]] == [
            "read",
            "search",
        ]
        assert payload["temperature"] == 0.1
        assert payload["max_tokens"] == 128

    def test_opencode_does_not_duplicate_caller_read_tool(self):
        payload = _provider()._build_payload(
            [{"role": "user", "content": "hi"}],
            0.1,
            128,
            tools=[{"type": "function", "function": {"name": "read"}}],
        )
        assert [t["function"]["name"] for t in payload["tools"]] == ["read"]

    def test_non_opencode_passes_tools_through(self):
        tools = [{"type": "function", "function": {"name": "search"}}]
        payload = _provider(OTHER)._build_payload(
            [{"role": "user", "content": "hi"}], 0.1, 128, tools=tools
        )
        assert payload["tools"] == tools


class TestStreamParsing:
    def test_text_content_variants(self):
        assert oc._text_content("hello") == "hello"
        assert oc._text_content(["a", {"text": "b"}, {"nope": 1}]) == "ab"
        assert oc._text_content(None) == ""

    def test_estimate_usage_has_minimum_one_token(self):
        usage = oc._estimate_usage("", "")
        assert usage.prompt_tokens == 1
        assert usage.completion_tokens == 1
        assert usage.total_tokens == 2

    def test_parse_sse_line_handles_noise(self):
        state = {"content": [], "usage": None, "finish_reason": "stop"}
        for line in ("", "  ", ": keepalive", "event: message", "data: [DONE]", "data: nope"):
            oc.OpencodeProvider._parse_sse_line(line, state)
        assert state["content"] == []

    def test_parse_sse_line_accumulates_content_usage_and_finish(self):
        state = {"content": [], "usage": None, "finish_reason": "stop"}
        oc.OpencodeProvider._parse_sse_line(
            'data: {"choices":[{"delta":{"content":"Hel"}}]}', state
        )
        oc.OpencodeProvider._parse_sse_line(
            'data: {"choices":[{"delta":{"content":[{"text":"lo"}]},"finish_reason":"length"}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
            state,
        )
        assert "".join(state["content"]) == "Hello"
        assert state["finish_reason"] == "length"
        assert state["usage"]["total_tokens"] == 5

    def test_parse_sse_line_uses_message_when_delta_absent(self):
        state = {"content": [], "usage": None, "finish_reason": "stop"}
        oc.OpencodeProvider._parse_sse_line(
            'data: {"choices":[{"message":{"content":"full"}}]}', state
        )
        assert state["content"] == ["full"]

    def test_parse_sse_line_ignores_non_dict_payloads(self):
        state = {"content": [], "usage": None, "finish_reason": "stop"}
        oc.OpencodeProvider._parse_sse_line("data: [1, 2]", state)
        oc.OpencodeProvider._parse_sse_line('data: {"choices": "nope"}', state)
        oc.OpencodeProvider._parse_sse_line('data: {"choices": ["x"]}', state)
        assert state["content"] == []

    def test_consume_lines_decodes_bytes(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
        ]
        state = _provider()._consume_lines(lines)
        assert "".join(state["content"]) == "ab"

    def test_consume_json_valid(self):
        body = json.dumps(
            {
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        ).encode()
        state = _provider()._consume_json(body)
        assert state["content"] == ["hello"]
        assert state["usage"]["total_tokens"] == 3

    def test_consume_json_without_choices(self):
        state = _provider()._consume_json(b'{"usage": {"total_tokens": 4}}')
        assert state["content"] == []
        assert state["usage"]["total_tokens"] == 4

    def test_consume_json_invalid_raises(self):
        with pytest.raises(GuardrailError, match="invalid JSON"):
            _provider()._consume_json(b"not json")

    def test_normalise_uses_reported_usage(self):
        response = _provider()._normalise(
            {
                "content": ["a", "b"],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
                "finish_reason": "stop",
            },
            "prompt",
        )
        assert response.content == "ab"
        assert response.usage.total_tokens == 11
        assert response.finish_reason == "stop"

    def test_normalise_estimates_when_usage_missing(self):
        response = _provider()._normalise(
            {"content": ["abcd"], "usage": None, "finish_reason": "length"}, "x" * 40
        )
        assert response.usage.prompt_tokens == 10
        assert response.usage.completion_tokens == 1
        assert response.finish_reason == "length"

    def test_parse_sse_line_accumulates_streaming_tool_call_fragments(self):
        state = {"content": [], "usage": None, "finish_reason": "stop"}
        oc.OpencodeProvider._parse_sse_line(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"function":{"name":"web_search","arguments":"{\\"query\\":\\"a"}}]}}]}',
            state,
        )
        oc.OpencodeProvider._parse_sse_line(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"}"}}]},"finish_reason":"tool_calls"}]}',
            state,
        )
        response = oc.OpencodeProvider._normalise(state, "prompt")
        assert len(response.tool_calls) == 1
        call = response.tool_calls[0]
        assert call.id == "call_1"
        assert call.name == "web_search"
        assert call.arguments == {"query": "a"}
        assert response.finish_reason == "tool_calls"
        assert response.raw["tool_calls"][0]["function"]["name"] == "web_search"

    def test_consume_json_parses_tool_calls(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "function": {
                                        "name": "compute_roas",
                                        "arguments": '{"revenue_usd": 10, "spend_usd": 2}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ).encode()
        response = oc.OpencodeProvider._normalise(_provider()._consume_json(body), "prompt")
        assert response.tool_calls[0].name == "compute_roas"
        assert response.tool_calls[0].arguments == {
            "revenue_usd": 10,
            "spend_usd": 2,
        }
        assert response.finish_reason == "tool_calls"

    def test_invalid_tool_arguments_are_preserved_for_the_model(self):
        state = {
            "content": [],
            "usage": None,
            "finish_reason": "tool_calls",
            "tool_calls": {0: {"id": "call_3", "name": "web_search", "arguments": "{bad"}},
        }
        response = oc.OpencodeProvider._normalise(state, "prompt")
        assert response.tool_calls[0].arguments == {"_raw_arguments": "{bad"}


class TestHttpxTransport:
    @respx.mock
    async def test_sse_stream(self):
        route = respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(_chunk("Hel"), _chunk("lo", "stop")),
            )
        )
        state = await _provider()._post_httpx({}, "ses_1")
        assert "".join(state["content"]) == "Hello"
        assert route.called
        request = route.calls[0].request
        assert request.headers["x-opencode-session"] == "ses_1"

    @respx.mock
    async def test_json_response(self):
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"choices": [{"message": {"content": "json reply"}}]},
            )
        )
        state = await _provider()._post_httpx({}, None)
        assert state["content"] == ["json reply"]

    @pytest.mark.parametrize(
        "headers, expected",
        [
            ({}, None),
            ({"Retry-After": "not-a-number"}, None),
            ({"Retry-After": "-5"}, None),
            ({"Retry-After": "0"}, 0.0),
        ],
    )
    def test_retry_after_header_parsing(self, headers, expected):
        assert oc._retry_after_seconds(headers) == expected

    @respx.mock
    async def test_429_is_non_retryable_quota_error_with_retry_after(self):
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                429, text="FreeUsageLimitError", headers={"Retry-After": "120"}
            )
        )
        with pytest.raises(LLMQuotaExceededError, match="429") as exc:
            await _provider()._post_httpx({}, None)
        assert exc.value.retry_after_seconds == 120.0
        assert isinstance(exc.value, LLMTransportError)

    @respx.mock
    async def test_500_is_transport_error(self):
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(503, text="overloaded")
        )
        with pytest.raises(LLMTransportError, match="503"):
            await _provider()._post_httpx({}, None)

    @respx.mock
    async def test_400_is_guardrail_error(self):
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        with pytest.raises(GuardrailError) as exc:
            await _provider()._post_httpx({}, None)
        assert not isinstance(exc.value, LLMTransportError)


class TestCurlTransport:
    def test_curl_transport_streams(self, monkeypatch):
        from curl_cffi import requests as curl_requests

        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            content = b""

            def iter_lines(self):
                return iter([b'data: {"choices":[{"delta":{"content":"curl"}}]}'])

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, headers=None, json=None):
                captured["method"] = method
                captured["url"] = url
                captured["headers"] = headers
                return FakeStream()

        monkeypatch.setattr(curl_requests, "Session", FakeSession)
        state = _provider()._post_curl({"model": "m"}, "ses_curl")
        assert "".join(state["content"]) == "curl"
        assert captured["url"] == f"{BASE}/chat/completions"
        assert captured["headers"]["x-opencode-session"] == "ses_curl"
        assert captured["http_version"] == "v2"

    def test_curl_transport_json(self, monkeypatch):
        from curl_cffi import requests as curl_requests

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = json.dumps({"choices": [{"message": {"content": "json"}}]}).encode()

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args, **kwargs):
                return FakeStream()

        monkeypatch.setattr(curl_requests, "Session", FakeSession)
        state = _provider()._post_curl({}, None)
        assert state["content"] == ["json"]

    @pytest.mark.parametrize("status,error_cls", [(429, LLMTransportError), (400, GuardrailError)])
    def test_curl_http_errors(self, monkeypatch, status, error_cls):
        from curl_cffi import requests as curl_requests

        class FakeResponse:
            status_code = status
            headers = {}
            content = b"nope"

        class FakeStream:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args, **kwargs):
                return FakeStream()

        monkeypatch.setattr(curl_requests, "Session", FakeSession)
        with pytest.raises(error_cls):
            _provider()._post_curl({}, None)


class TestRequestAndChat:
    async def test_request_wraps_httpx_errors(self, monkeypatch):
        async def boom(payload, session_id):
            raise httpx.ConnectError("connection refused")

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_post_httpx", boom)
        with pytest.raises(LLMTransportError, match="connection refused"):
            await provider._request({}, None)

    async def test_request_wraps_unknown_errors(self, monkeypatch):
        async def boom(payload, session_id):
            raise ValueError("weird")

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_post_httpx", boom)
        with pytest.raises(LLMTransportError, match="ValueError"):
            await provider._request({}, None)

    async def test_request_does_not_wrap_guardrail_errors(self, monkeypatch):
        async def boom(payload, session_id):
            raise GuardrailError("bad model")

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_post_httpx", boom)
        with pytest.raises(GuardrailError, match="bad model"):
            await provider._request({}, None)

    async def test_chat_returns_content(self, monkeypatch):
        async def fake_request(payload, session_id):
            return {
                "content": ["hello"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "finish_reason": "stop",
            }

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_request", fake_request)
        response = await provider.chat([{"role": "user", "content": "hi"}])
        assert response.content == "hello"
        assert response.usage.total_tokens == 2

    async def test_chat_retries_once_when_model_returns_empty_content(self, monkeypatch):
        seen = []

        async def fake_request(payload, session_id):
            seen.append([dict(m) for m in payload["messages"]])
            if len(seen) == 1:
                return {"content": [], "usage": None, "finish_reason": "tool_calls"}
            return {"content": ["recovered"], "usage": None, "finish_reason": "stop"}

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_request", fake_request)
        response = await provider.chat([{"role": "user", "content": "hi"}])
        assert response.content == "recovered"
        assert len(seen) == 2
        assert "Tool calls are unavailable" in seen[1][-1]["content"]

    async def test_chat_returns_empty_after_corrective_round(self, monkeypatch):
        async def fake_request(payload, session_id):
            return {"content": [], "usage": None, "finish_reason": "tool_calls"}

        provider = _provider(OTHER)
        monkeypatch.setattr(provider, "_request", fake_request)
        response = await provider.chat([{"role": "user", "content": "hi"}])
        assert response.content == ""

    async def test_chat_generates_session_for_opencode(self, monkeypatch):
        captured = {}

        async def fake_request(payload, session_id):
            captured["session"] = session_id
            captured["tool"] = payload["tools"][0]["function"]["name"]
            return {"content": ["ok"], "usage": None, "finish_reason": "stop"}

        provider = _provider()
        monkeypatch.setattr(provider, "_request", fake_request)
        await provider.chat([{"role": "user", "content": "hi"}])
        assert captured["session"].startswith("ses_")
        assert captured["tool"] == "read"

    async def test_headers_for_test_property(self):
        headers = _provider().headers_for_test
        assert headers["x-opencode-session"].startswith("ses_")
