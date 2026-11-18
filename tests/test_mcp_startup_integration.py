"""Integration test: every first-party MCP server boots over stdio.

The graph discovers these servers as subprocesses at runtime, so a packaging
regression in any server module only shows up when the process is spawned. This
test performs the real MCP handshake (``initialize`` → ``tools/list``) for every
server in ``adforge.mcp_servers.runner.SERVERS`` and pins the required tools.

No network or credentials are needed: external ad-platform MCPs are optional and
covered by the client-wiring tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "research": {
        "web_search",
        "wikipedia_search",
        "serp_scrape",
        "keyword_clusters",
        "competitor_creatives",
        "trends_interest",
        "fetch_page",
    },
    "ads": {"list_channels", "publish_campaign", "pause_campaign", "update_budget"},
    "analytics": {"persist_kpi_snapshot", "query_kpis", "aggregate_kpis"},
    "roi": {
        "compute_roas",
        "compute_cpa",
        "compute_payback_period",
        "classify_recommendation",
    },
    "compliance": {
        "evaluate_creative",
        "list_blocked_terms",
        "list_required_disclosures",
    },
    "linkedin": {"get_ad_analytics"},
    "public_data": {
        "similarweb_traffic",
        "appstore_search",
        "appstore_reviews",
        "ipinfo_lookup",
        "abuseipdb_check",
        "github_repo_stats",
        "hackernews_top",
        "reddit_search",
        "stackexchange_search",
        "producthunt_search",
        "opencorporates_search",
        "wikipedia_search_direct",
        "sec_financials",
        "wikipedia_pageviews",
    },
}

#: Tools that must never be exposed to an LLM-facing caller.
EXPECTED_WRITE_TOOLS = {
    "ads": {"publish_campaign", "pause_campaign", "update_budget"},
    # Internal persistence is a write tool even though it does not need HITL.
    "analytics": {"persist_kpi_snapshot"},
}


def _read_line(proc: subprocess.Popen[str], timeout: float = 30.0) -> str:
    """Read one stdout line without letting a hung server block forever."""
    result: dict[str, str] = {}

    def _read() -> None:
        result["line"] = proc.stdout.readline() if proc.stdout else ""

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"server did not respond within {timeout}s")
    return result.get("line", "")


def _read_json(proc: subprocess.Popen[str], request_id: int) -> dict:
    while True:
        line = _read_line(proc)
        if not line:
            raise RuntimeError("server closed stdout before replying")
        text = line.strip()
        if not text.startswith("{"):
            continue
        payload = json.loads(text)
        if payload.get("id") == request_id:
            return payload


def _send(proc: subprocess.Popen[str], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _start_server(name: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env.update({"MCP_TRANSPORT": "stdio", "MCP_ENABLED": "true"})
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), env.get("PYTHONPATH", "")) if value
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "adforge.mcp_servers.runner", name],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )


@pytest.mark.parametrize("server", sorted(EXPECTED_TOOLS))
def test_server_starts_and_exposes_required_tools(server):
    proc = _start_server(server)
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "adforge-startup-test", "version": "1.0"},
                },
            },
        )
        init = _read_json(proc, 1)
        assert "error" not in init, init
        assert init["result"]["serverInfo"]["name"]

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _read_json(proc, 2)
        assert "error" not in listed, listed

        actual = {tool["name"] for tool in listed["result"]["tools"]}
        assert actual == EXPECTED_TOOLS[server]

        annotations = {
            tool["name"]: tool.get("annotations") or {}
            for tool in listed["result"]["tools"]
        }
        writes = EXPECTED_WRITE_TOOLS.get(server, set())
        for name, hints in annotations.items():
            if name in writes:
                # Write tools must be explicitly destructive/not read-only so
                # the client's fail-closed filter excludes them.
                assert hints.get("readOnlyHint") is False, (server, name, hints)
                assert hints.get("destructiveHint") is True, (server, name, hints)
            else:
                # Everything else is a read tool and must say so explicitly.
                assert hints.get("readOnlyHint") is True, (server, name, hints)
                assert hints.get("destructiveHint") is not True, (server, name, hints)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate(timeout=5)
        if proc.returncode not in (0, -15, 1, 3221225786):
            pytest.fail(f"{server} exited with {proc.returncode}: {stderr[-2000:]}")
