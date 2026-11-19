"""Tests for the MCP client wiring (``adforge.mcp_servers.client``).

The real ``MultiServerMCPClient`` would spawn subprocesses / open network
connections, so every test replaces the class imported by ``_build_client``
with a recording fake. Settings are reset around each test through the shared
autouse fixture in ``conftest.py``; tests that override env clear the settings
cache themselves.
"""

from __future__ import annotations

import logging
import sys

import pytest

from adforge.config import get_settings
from adforge.mcp_servers import client as mcp_client

LOCAL_SERVERS = (
    "research",
    "ads",
    "analytics",
    "roi",
    "compliance",
    "public_data",
    "linkedin",
)

EXPECTED_PORTS = {
    "research": 9001,
    "ads": 9002,
    "analytics": 9003,
    "roi": 9004,
    "compliance": 9005,
    "public_data": 9007,
    "linkedin": 9012,
}


class FakeTool:
    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = metadata


class FakeMultiServerMCPClient:
    """Records the connection map and serves deterministic tool lists."""

    instances: list[FakeMultiServerMCPClient] = []

    def __init__(self, servers):
        self.servers = servers
        self.tool_calls: list[str] = []
        FakeMultiServerMCPClient.instances.append(self)

    async def get_tools(self, *, server_name):
        # Mirror the installed langchain-mcp-adapters signature, which is
        # keyword-only. A positional call must fail loudly.
        self.tool_calls.append(server_name)
        return [
            FakeTool(
                f"{server_name}:read",
                {"readOnlyHint": True, "destructiveHint": False},
            ),
            FakeTool(
                f"{server_name}:write",
                {"readOnlyHint": False, "destructiveHint": True},
            ),
            FakeTool(f"{server_name}:unannotated"),
        ]


class RaisingMultiServerMCPClient:
    """A client whose tool lookup always fails (unreachable server)."""

    def __init__(self, servers):
        self.servers = servers

    async def get_tools(self, *, server_name):
        raise RuntimeError(f"{server_name} is down")


@pytest.fixture
def fake_mcp(monkeypatch):
    """Patch the MCP client class and clear the module-level lru_cache."""
    import langchain_mcp_adapters.client as lc_client

    FakeMultiServerMCPClient.instances.clear()
    monkeypatch.setattr(lc_client, "MultiServerMCPClient", FakeMultiServerMCPClient, raising=False)
    mcp_client.reset_mcp_client()
    yield FakeMultiServerMCPClient
    mcp_client.reset_mcp_client()


def _prepare_env(monkeypatch, **overrides) -> None:
    """Set MCP-related env vars deterministically and reset settings."""
    values = {
        "MCP_ENABLED": "true",
        "MCP_TRANSPORT": "stdio",
        "PIPEBOARD_API_TOKEN": "",
        "GOOGLE_APPLICATION_CREDENTIALS": "",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


class TestBuildClient:
    def test_stdio_is_default_and_has_seven_local_servers(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_TRANSPORT="stdio")

        client = mcp_client._build_client()
        servers = client.servers

        assert isinstance(client, FakeMultiServerMCPClient)
        assert set(servers) == set(LOCAL_SERVERS)
        for name in LOCAL_SERVERS:
            cfg = servers[name]
            assert cfg["transport"] == "stdio"
            assert cfg["command"] == sys.executable
            assert cfg["args"] == ["-m", "adforge.mcp_servers.runner", name]

    def test_sse_transport_uses_localhost_ports(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_TRANSPORT="sse")

        servers = mcp_client._build_client().servers

        assert set(servers) == set(LOCAL_SERVERS)
        for name, port in EXPECTED_PORTS.items():
            assert servers[name]["transport"] == "sse"
            assert servers[name]["url"] == f"http://localhost:{port}/sse"
        # Spot-check a couple of the configured ports explicitly.
        assert servers["research"]["url"].endswith(":9001/sse")
        assert servers["roi"]["url"].endswith(":9004/sse")
        assert servers["public_data"]["url"].endswith(":9007/sse")
        assert servers["linkedin"]["url"].endswith(":9012/sse")

    def test_pipeboard_token_adds_remote_sse_servers(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, PIPEBOARD_API_TOKEN="pb-secret-token")

        servers = mcp_client._build_client().servers

        assert set(servers) == set(LOCAL_SERVERS) | {"meta", "tiktok", "snap", "reddit"}
        for name in ("meta", "tiktok", "snap", "reddit"):
            assert servers[name]["transport"] == "sse"
            assert servers[name]["headers"] == {"Authorization": "Bearer pb-secret-token"}
            assert servers[name]["url"].startswith("https://")
            assert servers[name]["url"].endswith("/sse")
        assert servers["meta"]["url"] == "https://meta-ads.mcp.pipeboard.co/sse"

    def test_google_credentials_add_official_streamable_http_server(self, monkeypatch, fake_mcp):
        _prepare_env(
            monkeypatch,
            GOOGLE_APPLICATION_CREDENTIALS="C:/secrets/google-creds.json",
        )

        servers = mcp_client._build_client().servers

        assert "google_ads_official" in servers
        assert servers["google_ads_official"] == {
            "transport": "streamable_http",
            "url": "http://localhost:9006/mcp",
        }
        assert set(servers) == set(LOCAL_SERVERS) | {"google_ads_official"}


class TestGetMcpTools:
    async def test_disabled_returns_empty_without_building_client(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_ENABLED="false")

        assert await mcp_client.get_mcp_tools("research") == []
        assert fake_mcp.instances == []

    async def test_enabled_returns_only_read_only_tools(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_ENABLED="true")

        tools = await mcp_client.get_mcp_tools("roi")

        assert [tool.name for tool in tools] == ["roi:read"]
        assert len(fake_mcp.instances) == 1
        assert fake_mcp.instances[0].tool_calls == ["roi"]

    async def test_read_only_false_returns_every_tool(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_ENABLED="true")

        tools = await mcp_client.get_mcp_tools("roi", read_only=False)

        assert [tool.name for tool in tools] == [
            "roi:read",
            "roi:write",
            "roi:unannotated",
        ]

    async def test_client_failure_returns_empty_and_logs(self, monkeypatch, caplog):
        import langchain_mcp_adapters.client as lc_client

        monkeypatch.setattr(
            lc_client,
            "MultiServerMCPClient",
            RaisingMultiServerMCPClient,
            raising=False,
        )
        mcp_client.reset_mcp_client()
        _prepare_env(monkeypatch, MCP_ENABLED="true")
        caplog.set_level(logging.WARNING, logger="adforge.mcp")

        try:
            tools = await mcp_client.get_mcp_tools("ads")
        finally:
            mcp_client.reset_mcp_client()

        assert tools == []
        assert "unavailable" in caplog.text
        assert "ads" in caplog.text


class TestResetMcpClient:
    def test_reset_clears_cached_client(self, monkeypatch, fake_mcp):
        _prepare_env(monkeypatch, MCP_TRANSPORT="stdio")

        first = mcp_client._build_client()
        again = mcp_client._build_client()
        assert first is again
        assert len(fake_mcp.instances) == 1

        mcp_client.reset_mcp_client()
        third = mcp_client._build_client()

        assert third is not first
        assert len(fake_mcp.instances) == 2
