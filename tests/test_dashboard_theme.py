"""Tests for the dashboard design system.

These pin the parts of ``adforge.dashboard.theme`` that are easy to regress
silently: escaping in the public HTML helpers, numeric-column alignment in the
ledger tables, and the Mermaid viewport-fit script that keeps diagrams legible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adforge.dashboard import theme

_DASHBOARD = Path(__file__).resolve().parents[1] / "adforge" / "dashboard"


def _rendered(monkeypatch: pytest.MonkeyPatch, func, *args, **kwargs) -> str:
    """Run a rendering helper and return the HTML it passed to st.markdown."""
    captured: list[str] = []
    monkeypatch.setattr(theme.st, "markdown", lambda body, **_: captured.append(body))
    func(*args, **kwargs)
    return "\n".join(captured)


class TestEmptyState:
    def test_escapes_all_user_supplied_text(self, monkeypatch):
        html = _rendered(
            monkeypatch,
            theme.af_empty,
            "<script>alert(1)</script>",
            "Body & more",
            hint="<b>make seed</b>",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "Body &amp; more" in html
        assert "&lt;b&gt;make seed&lt;/b&gt;" in html

    def test_kicker_defaults_to_a_readable_label(self, monkeypatch):
        html = _rendered(monkeypatch, theme.af_empty, "Title", "Body")
        assert "Nothing here yet" in html


class TestLedgerTable:
    def test_right_aligns_only_the_numeric_columns(self):
        table = theme.af_table(
            ["Agent", "Tokens"],
            [["research", "<b>1,234</b>"]],
            numeric_cols={1},
        )
        assert "<th>Agent</th>" in table
        assert '<th class="af-num">Tokens</th>' in table
        assert "<td>research</td>" in table
        assert '<td class="af-num"><b>1,234</b></td>' in table

    def test_escapes_headers_but_keeps_trusted_cell_html(self):
        table = theme.af_table(
            ["<script>", "Status"],
            [[theme.af_badge("approved"), "ok"]],
        )
        assert "&lt;script&gt;" in table
        assert "<script>" not in table
        assert 'class="af-badge af-badge--approved"' in table


class TestVerdictBanner:
    def test_renders_word_stats_and_note_in_the_tone_colour(self, monkeypatch):
        html = _rendered(
            monkeypatch,
            theme.af_verdict,
            "KILL",
            "failed",
            [("Blended ROAS", "0.82x"), ("Net profit", "-$1,951.67")],
            note="Below break-even.",
            note_label="Why this verdict.",
        )
        assert theme.STATUS_COLORS["failed"] in html
        assert "KILL" in html
        assert "0.82x" in html
        assert "-$1,951.67" in html
        assert "Why this verdict." in html
        assert "Below break-even." in html

    def test_escapes_agent_prose(self, monkeypatch):
        html = _rendered(
            monkeypatch,
            theme.af_verdict,
            "PAUSE",
            "blocked",
            [("ROAS", "1.10x")],
            note="<img src=x onerror=alert(1)>",
        )
        assert "<img" not in html
        assert "&lt;img" in html


class TestMermaidRenderer:
    def test_fits_the_svg_to_the_frame_and_disables_scrolling(self, monkeypatch):
        captured: list[tuple[str, dict]] = []

        def fake_html(doc: str, **kwargs) -> None:
            captured.append((doc, kwargs))

        monkeypatch.setattr(theme.st.components.v1, "html", fake_html)
        theme.render_mermaid('flowchart LR\n    A["Brief"] --> B["Launch"]', 420)

        doc, kwargs = captured[0]
        assert "preserveAspectRatio" in doc
        assert "xMidYMid meet" in doc
        assert kwargs == {"height": 420, "scrolling": False}
        assert "data-af-diagram-error" in doc

    def test_embeds_the_source_as_json(self, monkeypatch):
        captured: list[str] = []
        monkeypatch.setattr(theme.st.components.v1, "html", lambda doc, **_: captured.append(doc))
        source = 'flowchart LR\n    A["He said \\"go\\""] --> B'
        theme.render_mermaid(source, 300)
        # Quotes and newlines are JSON-escaped into the component script.
        assert 'A["He said' not in captured[0]
        assert "He said" in captured[0]
        assert "\\n    A[" in captured[0]


class TestSharedStylesheet:
    def test_pages_do_not_ship_page_local_styles(self):
        pages = sorted((_DASHBOARD / "pages").glob("*.py"))
        assert pages
        offenders = [p.name for p in pages if "<style>" in p.read_text(encoding="utf-8")]
        assert offenders == []

    def test_button_labels_inherit_the_button_colour(self):
        # The global prose colour would otherwise paint primary-button labels
        # slate on pine, which made the Run button look empty.
        assert ".stButton > button p" in theme._CSS

    def test_overview_is_the_entrypoint_so_the_nav_says_overview(self):
        assert (_DASHBOARD / "Overview.py").exists()
        assert not (_DASHBOARD / "app.py").exists()
