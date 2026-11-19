"""OpenAI-compatible LLM provider with first-class opencode.ai support.

The opencode.ai free endpoint (``https://opencode.ai/inference/openai/v1``)
is gated on four things:

    1. a Bun-compatible TLS fingerprint (via ``curl_cffi``),
    2. an ``<env>`` block inside the system prompt,
    3. a nonzero OpenCode session id sent as request headers,
    4. a tool named ``read`` present in the request.

This module mirrors the battle-tested client in
``D:/DeepSeek_Chat_Research/pipeline/llm_endpoint.py`` but exposes the plain
``LLMProvider`` interface used by every AdForge agent.

For any other OpenAI-compatible gateway, the same provider works over plain
``httpx`` — set ``LLM_BASE_URL`` to your gateway and leave the opencode
quirks dormant.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from adforge.config import get_settings
from adforge.guardrails.exceptions import (
    GuardrailError,
    LLMQuotaExceededError,
    LLMTransportError,
)
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage, ToolCall

_OPENCODE_DEFAULT_USER_AGENT = "opencode/latest/2.2.5/cli"
_SESSION_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SESSION_COUNTER_LIMIT = 1 << 12
_SESSION_LOW_BITS = (1 << 48) - 1
_OPENCODE_ENV_RE = re.compile(r"<env>.*?</env>", re.DOTALL | re.IGNORECASE)


def _retry_after_seconds(headers: Any) -> float | None:
    """Parse a numeric ``Retry-After`` header, if the provider sent one."""
    raw = None
    if headers:
        raw = next(
            (value for key, value in headers.items() if str(key).lower() == "retry-after"),
            None,
        )
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _raise_http_error(status: int, body: str, headers: Any) -> None:
    """Map an HTTP failure to the right typed guardrail exception."""
    message = f"LLM endpoint HTTP {status}: {body[:500]}"
    if status == 429:
        raise LLMQuotaExceededError(message, _retry_after_seconds(headers))
    if status >= 500:
        raise LLMTransportError(message)
    raise GuardrailError(message)


# Bun's TLS ClientHello, as sent by the OpenCode CLI (JA3 md5
# c38fd2ef2e433bf382d6510bf8c311a6). Required by the free endpoint.
_BUN_JA3 = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171-"
    "49162-49172-156-157-47-53,0-23-65281-10-11-35-16-13-51-45-43-21,"
    "29-23-24,0"
)
_BUN_SIGNATURE_ALGORITHMS = (
    "ecdsa_secp256r1_sha256",
    "rsa_pss_rsae_sha256",
    "rsa_pkcs1_sha256",
    "ecdsa_secp384r1_sha384",
    "rsa_pss_rsae_sha384",
    "rsa_pkcs1_sha384",
    "rsa_pss_rsae_sha512",
    "rsa_pkcs1_sha512",
    "rsa_pkcs1_sha1",
)

_last_session_ms = 0
_session_counter = 0
_session_lock = threading.Lock()

# The free endpoint rejects requests without a tool named "read".
_READ_GATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read",
        "description": (
            "Reserved OpenCode capability marker. This client does not "
            "provide file reading; answer directly without calling tools."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def generate_session_id(now_ms: int | None = None) -> str:
    """Mint an OpenCode-compatible session id (``ses_...``).

    Mirrors the CLI generator: the low 48 bits of ``ms * 4096 + counter``
    are complemented and rendered as 12 hex chars, followed by 14 random
    base62 chars. Counters start at 1 — the endpoint rejects 0.
    """
    global _last_session_ms, _session_counter

    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ms < 0:
        raise ValueError("now_ms must be non-negative")

    with _session_lock:
        if ms > _last_session_ms:
            _last_session_ms = ms
            _session_counter = 1
        else:
            _session_counter += 1
            if _session_counter >= _SESSION_COUNTER_LIMIT:
                _last_session_ms += 1
                _session_counter = 1
        ms = _last_session_ms
        counter = _session_counter

    encoded = ~(ms * _SESSION_COUNTER_LIMIT + counter) & _SESSION_LOW_BITS
    tail = "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(14))
    return f"ses_{encoded:012x}{tail}"


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _estimate_usage(prompt_text: str, completion_text: str) -> LLMUsage:
    prompt_tokens = max(1, len(prompt_text) // 4)
    completion_tokens = max(1, len(completion_text) // 4)
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


class OpencodeProvider(LLMProvider):
    """OpenAI-compatible chat provider (opencode.ai free tier aware)."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.llm_base_url).rstrip("/")
        self.model = model or s.llm_model
        self.api_key = api_key or s.llm_api_key
        self.timeout = timeout or s.llm_timeout
        if not self.base_url or not self.model or not self.api_key:
            raise GuardrailError(
                "OpenAI-compatible endpoint is not configured — set "
                "LLM_BASE_URL, LLM_MODEL and LLM_API_KEY (or LLM_PROVIDER=glm)."
            )
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise GuardrailError("LLM_BASE_URL must be an absolute http(s) URL")

    # ------------------------------------------------------------------ #
    # Wire format helpers
    # ------------------------------------------------------------------ #

    @property
    def is_opencode(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host == "opencode.ai" or host.endswith(".opencode.ai")

    @property
    def chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self, session_id: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if not self.is_opencode:
            return headers
        headers["User-Agent"] = _OPENCODE_DEFAULT_USER_AGENT
        headers["x-opencode-client"] = "cli"
        if session_id:
            headers["x-opencode-session"] = session_id
            headers["x-opencode-session-id"] = session_id
            headers["x-session-affinity"] = session_id
            headers["x-session-id"] = session_id
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        parent_id = secrets.token_hex(8)
        headers["b3"] = f"{trace_id}-{span_id}-1-{parent_id}"
        headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
        return headers

    def _prepare_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        prepared = [dict(m) for m in messages]
        if not self.is_opencode:
            return prepared

        env = (
            "<env>\n"
            "  Working directory: /workspace\n"
            "  Workspace root folder: /workspace\n"
            "  Is directory a git repo: no\n"
            "  Platform: linux\n"
            "</env>"
        )
        for message in prepared:
            if message.get("role") == "system":
                content = message.get("content") or ""
                if isinstance(content, str) and not _OPENCODE_ENV_RE.search(content):
                    message["content"] = f"{content.strip()}\n\n{env}"
                return prepared
        prepared.insert(0, {"role": "system", "content": f"You are a helpful assistant.\n\n{env}"})
        return prepared

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        s = get_settings()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature if temperature is not None else s.llm_temperature,
            "max_tokens": max_tokens or s.llm_max_tokens,
        }
        caller_tools = [dict(tool) for tool in (tools or [])]
        if self.is_opencode:
            # The free endpoint rejects requests that do not include a tool
            # named "read". Keep the gate first, then expose the caller's real
            # (read-only) tools so the model can actually use them.
            names = {str((tool.get("function") or {}).get("name", "")) for tool in caller_tools}
            payload["tools"] = ([] if "read" in names else [dict(_READ_GATE_TOOL)]) + caller_tools
        elif caller_tools:
            payload["tools"] = caller_tools
        return payload

    # ------------------------------------------------------------------ #
    # Response parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _accumulate_tool_calls(state: dict[str, Any], raw_calls: Any) -> None:
        """Merge streaming tool-call fragments into ``state['tool_calls']``."""
        if not isinstance(raw_calls, list):
            return
        accumulated = state.setdefault("tool_calls", {})
        for position, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                continue
            index = raw.get("index", position)
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = position
            entry = accumulated.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = raw.get("id")
            if call_id:
                entry["id"] = str(call_id)
            function = raw.get("function") or {}
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if name:
                entry["name"] = str(name)
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                entry["arguments"] += arguments
            elif isinstance(arguments, dict):
                entry["arguments"] += json.dumps(arguments)

    @staticmethod
    def _parse_sse_line(line: str, state: dict[str, Any]) -> None:
        line = line.strip()
        if not line or line.startswith((":", "event:")):
            return
        data = line[5:].strip() if line.startswith("data:") else line
        if data == "[DONE]" or not data:
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if isinstance(event.get("usage"), dict):
            state["usage"] = event["usage"]
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0]
        if not isinstance(choice, dict):
            return
        delta = choice.get("delta") or choice.get("message") or {}
        if isinstance(delta, dict):
            state["content"].append(_text_content(delta.get("content")))
            if "tool_calls" in delta:
                OpencodeProvider._accumulate_tool_calls(state, delta.get("tool_calls"))
        if choice.get("finish_reason"):
            state["finish_reason"] = str(choice["finish_reason"])

    def _consume_lines(self, lines: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            "content": [],
            "usage": None,
            "finish_reason": "stop",
            "tool_calls": {},
        }
        for raw_line in lines:
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            self._parse_sse_line(str(raw_line), state)
        return state

    def _consume_json(self, body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise GuardrailError("LLM endpoint returned invalid JSON") from exc
        choices = payload.get("choices") or []
        if not choices:
            return {
                "content": [],
                "usage": payload.get("usage"),
                "finish_reason": "stop",
                "tool_calls": {},
            }
        message = choices[0].get("message") or {}
        state: dict[str, Any] = {
            "content": [_text_content(message.get("content"))],
            "usage": payload.get("usage"),
            "finish_reason": choices[0].get("finish_reason") or "stop",
            "tool_calls": {},
        }
        self._accumulate_tool_calls(state, message.get("tool_calls"))
        return state

    @staticmethod
    def _normalise(state: dict[str, Any], prompt_text: str) -> LLMResponse:
        content = "".join(part for part in state["content"] if part)
        usage_raw = state.get("usage") or {}
        if usage_raw:
            usage = LLMUsage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
                total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
            )
        else:
            usage = _estimate_usage(prompt_text, content)

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for index, entry in sorted(
            (state.get("tool_calls") or {}).items(), key=lambda item: item[0]
        ):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            raw_arguments = entry.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
            except (TypeError, ValueError):
                arguments = {"_raw_arguments": str(raw_arguments)}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            call_id = str(entry.get("id") or f"call_{index}")
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
            raw_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            )

        finish_reason = str(state.get("finish_reason", "stop"))
        if tool_calls and finish_reason in ("stop", "length", ""):
            finish_reason = "tool_calls"

        return LLMResponse(
            content=content,
            usage=usage,
            raw={
                "finish_reason": finish_reason,
                "tool_calls": raw_tool_calls,
            },
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------------ #
    # Transports
    # ------------------------------------------------------------------ #

    async def _post_httpx(self, payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout, connect=min(20.0, self.timeout))
        async with (
            httpx.AsyncClient(timeout=timeout, trust_env=False) as client,
            client.stream(
                "POST",
                self.chat_completions_url,
                headers=self._headers(session_id),
                json=payload,
            ) as response,
        ):
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                _raise_http_error(response.status_code, body, response.headers)
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                return self._consume_json(await response.aread())
            state: dict[str, Any] = {
                "content": [],
                "usage": None,
                "finish_reason": "stop",
                "tool_calls": {},
            }
            async for line in response.aiter_lines():
                self._parse_sse_line(line, state)
            return state

    def _post_curl(self, payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        from curl_cffi import requests as curl_requests

        session_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "trust_env": False,
            "allow_redirects": False,
            "http_version": "v2",
        }
        try:
            from curl_cffi.requests.impersonate import ExtraFingerprints

            session_kwargs.update(
                ja3=_BUN_JA3,
                extra_fp=ExtraFingerprints(
                    tls_signature_algorithms=list(_BUN_SIGNATURE_ALGORITHMS),
                    tls_grease=False,
                    tls_permute_extensions=False,
                ),
            )
        except (ImportError, AttributeError):  # pragma: no cover - version guard
            session_kwargs.update(impersonate="chrome", http_version="v1")

        with (
            curl_requests.Session(**session_kwargs) as session,
            session.stream(
                "POST",
                self.chat_completions_url,
                headers=self._headers(session_id),
                json=payload,
            ) as response,
        ):
            if response.status_code >= 400:
                body = response.content.decode("utf-8", errors="replace")
                _raise_http_error(response.status_code, body, response.headers)
            content_type = str(response.headers.get("content-type", "")).lower()
            if "application/json" in content_type:
                return self._consume_json(response.content)
            return self._consume_lines(response.iter_lines())

    async def _request(self, payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        transport = "curl_cffi" if self.is_opencode else "httpx"
        try:
            if transport == "curl_cffi":
                return await asyncio.to_thread(self._post_curl, payload, session_id)
            return await self._post_httpx(payload, session_id)
        except GuardrailError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise LLMTransportError(f"LLM request failed: {exc}") from exc
        except Exception as exc:  # curl_cffi errors are not httpx errors
            raise LLMTransportError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        session_id = generate_session_id() if self.is_opencode else None
        payload = self._build_payload(messages, temperature, max_tokens, tools=tools)
        prompt_text = " ".join(str(m.get("content", "")) for m in messages)

        # One corrective round only when the model returned neither content nor
        # a tool call. A real tool call must be handed back to the agent loop.
        for attempt in range(2):
            state = await self._request(payload, session_id)
            response = self._normalise(state, prompt_text)
            if response.content.strip() or response.tool_calls:
                return response
            if attempt == 0:
                payload["messages"] = [
                    *payload["messages"],
                    {
                        "role": "user",
                        "content": (
                            "Tool calls are unavailable in this client. Reply "
                            "directly with the requested JSON content."
                        ),
                    },
                ]
                continue
            return response
        return response  # pragma: no cover - loop always returns

    # Exposed for tests
    @property
    def headers_for_test(self) -> dict[str, str]:
        return self._headers(generate_session_id())
