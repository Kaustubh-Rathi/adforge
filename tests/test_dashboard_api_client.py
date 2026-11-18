"""Tests for the dashboard's API client: tenant scoping and header shaping.

The dashboard is the only consumer of these helpers, and a wrong tenant means
the demo shows an empty console, so the selection logic is pinned here without
needing a running Streamlit server.
"""
from __future__ import annotations

import pytest

from adforge.dashboard import api_client


class _FakeSessionState(dict):
    """Stand-in for ``st.session_state`` (a dict-like attribute bag)."""


class _FakeStreamlit:
    def __init__(self, state: _FakeSessionState | None = None) -> None:
        self.session_state = state if state is not None else _FakeSessionState()
        self.sidebar = self
        self.calls: list[tuple[str, object]] = []

    def selectbox(self, label, options, key=None, help=None):
        self.calls.append(("selectbox", {"label": label, "key": key, "help": help}))
        return self.session_state.get(key)


@pytest.fixture
def fake_st(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setitem(__import__("sys").modules, "streamlit", fake)
    return fake


class TestTenantSelection:
    def test_defaults_to_the_configured_tenant(self, fake_st):
        assert api_client.current_tenant() == api_client.DEFAULT_TENANT

    def test_reads_the_sidebar_selection(self, fake_st):
        fake_st.session_state["tenant"] = "tenant_acme_saas"
        assert api_client.current_tenant() == "tenant_acme_saas"

    def test_falls_back_outside_streamlit(self, monkeypatch):
        """Importable from CLI/tests where no Streamlit runtime exists."""
        monkeypatch.setitem(__import__("sys").modules, "streamlit", None)
        assert api_client.current_tenant() == api_client.DEFAULT_TENANT

    def test_seeded_tenants_are_offered(self):
        # The demo seeds one campaign per tenant; all of them must be reachable.
        for tenant in ("tenant_acme_saas", "tenant_bloom_bistro", "tenant_vertex_audio"):
            assert tenant in api_client.KNOWN_TENANTS


class TestTenantSwitcher:
    def test_initialises_session_state_and_renders_a_selectbox(self, fake_st):
        api_client.tenant_switcher()
        assert fake_st.session_state["tenant"] == api_client.DEFAULT_TENANT
        assert fake_st.calls[0][1]["key"] == "tenant"

    def test_keeps_an_existing_valid_selection(self, fake_st):
        fake_st.session_state["tenant"] = "tenant_vertex_audio"
        api_client.tenant_switcher()
        assert fake_st.session_state["tenant"] == "tenant_vertex_audio"

    def test_resets_an_unknown_selection(self, fake_st):
        """A stale value from a previous deploy must not 404 every request."""
        fake_st.session_state["tenant"] = "tenant_that_no_longer_exists"
        api_client.tenant_switcher()
        assert fake_st.session_state["tenant"] == api_client.DEFAULT_TENANT


class TestHeaders:
    def test_headers_carry_the_selected_tenant(self, fake_st):
        fake_st.session_state["tenant"] = "tenant_bloom_bistro"
        headers = api_client._headers()
        assert headers["X-AdForge-Tenant"] == "tenant_bloom_bistro"

    def test_api_key_is_sent_only_when_configured(self, fake_st, monkeypatch):
        monkeypatch.setattr(api_client, "API_KEY", "")
        assert "X-API-Key" not in api_client._headers()

        monkeypatch.setattr(api_client, "API_KEY", "secret")
        assert api_client._headers()["X-API-Key"] == "secret"

    def test_extra_headers_do_not_clobber_the_tenant(self, fake_st):
        headers = api_client._headers({"X-AdForge-Reviewer": "rev@example.com"})
        assert headers["X-AdForge-Tenant"] == api_client.DEFAULT_TENANT
        assert headers["X-AdForge-Reviewer"] == "rev@example.com"
