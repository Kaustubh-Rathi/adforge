"""AdForge dashboard design system.

One source of truth for the dashboard's visual language: the CSS layer, the
brand/status colour tokens, and small HTML primitives that every page reuses
(masthead, status badges, figure rows, section headers, legends) plus the
Mermaid renderer used for the architecture diagrams.

Design identity (de-slop pass, 2026-11)
--------------------------------------
The product is an ad-campaign operations console for performance marketers,
so the look is quiet and operational, not SaaS-marketing: one ink masthead
bar, JetBrains Mono for figures and machine names (the page's personality),
Inter for prose, hairline rules instead of floating rounded cards, a pine
green accent reserved for interactive state (red stays reserved for blocked
/ failed), one border radius, no shadows except the masthead, and exactly
one orchestrated reveal (the masthead itself).

Usage from ``app.py`` or any file in ``adforge/dashboard/pages/``::

    from adforge.dashboard.theme import (
        af_badge,
        af_card,
        af_legend,
        af_metric,
        af_section,
        inject_css,
        render_mermaid,
    )

    inject_css()          # first call, right after st.set_page_config()
    st.markdown(af_card("research", "Ship it", "One line of copy."),
                unsafe_allow_html=True)
    render_mermaid(diagram_source, 620)

Conventions
-----------
* Everything that returns an ``str`` is a *fragment*: pass it to
  ``st.markdown(..., unsafe_allow_html=True)``. Everything that returns
  ``None`` renders itself.
* Status tokens (see :data:`STATUS_COLORS`) drive badges, legends and diagram
  palettes so a campaign looks identical everywhere.
* Streamlit renders custom HTML inside a ``<div>`` tree, so all rules are
  scoped to ``af-`` classes or to selectors Streamlit really emits
  (``h1``-``h3``, ``[data-testid="stMetric"]``, ``.stButton>button``,
  ``.stExpander``, ``[data-testid="stSidebar"]``, ``.block-container``).
  ``:has()`` is deliberately avoided for compatibility.
"""
from __future__ import annotations

import html
import json
import re

import streamlit as st

# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
# Interaction accent: pine green. Focus rings, active nav, links, primary
# buttons. Signal red is deliberately NOT an accent here - red means "blocked"
# in this product's own status vocabulary.
ACCENT = "#176B4F"
ACCENT_HOVER = "#11533E"
ACCENT_SOFT = "#E9F2ED"
HEADING = "#12181F"   # ink
BODY = "#3B4552"      # slate body copy
MUTED = "#6B7683"     # labels, captions (AA on both surfaces)
BORDER = "#D9DFE5"    # hairlines
SURFACE = "#FFFFFF"
CANVAS = "#F3F5F7"    # cool paper, not cream
INK = "#12181F"       # masthead / terminal surfaces

#: Canonical status -> colour. Used by badges, legends, charts and diagrams.
STATUS_COLORS: dict[str, str] = {
    "draft": "#6B7683",
    "designing": "#6D28D9",
    "reviewing": "#B45309",
    "approved": "#176B4F",
    "blocked": "#B42318",
    "failed": "#991B1B",
    "mock": "#B45309",
}

#: Statuses the API may return that are not in the token list, mapped onto the
#: closest token so pages never show a grey pill for a known state.
STATUS_ALIASES: dict[str, str] = {
    "live": "approved",
    "launched": "approved",
    "running": "approved",
    "pending": "reviewing",
    "awaiting_review": "reviewing",
    "in_review": "reviewing",
    "rejected": "blocked",
    "auto_blocked": "blocked",
    "error": "failed",
    "planning": "designing",
}


def status_color(status: str) -> str:
    """Return the brand colour for ``status`` (slate when unknown)."""
    key = (status or "").strip().lower()
    token = STATUS_ALIASES.get(key, key)
    return STATUS_COLORS.get(token, MUTED)


def status_class(status: str) -> str:
    """Return the CSS modifier (``approved``) used by ``.af-badge--<token>``."""
    token = STATUS_ALIASES.get((status or "").strip().lower(), (status or "").strip().lower())
    if token in STATUS_COLORS:
        return token
    return token if re.fullmatch(r"[a-z0-9_-]{1,24}", token) else "slate"


def _esc(text: object) -> str:
    """HTML-escape any value for safe inclusion in a fragment."""
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    "?family=Inter:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@500;700;800&display=swap\">"
)

_CSS = """
:root {
  --af-accent: #176B4F;
  --af-accent-hover: #11533E;
  --af-accent-soft: #E9F2ED;
  --af-heading: #12181F;
  --af-body: #3B4552;
  --af-muted: #6B7683;
  --af-border: #D9DFE5;
  --af-surface: #FFFFFF;
  --af-canvas: #F3F5F7;
  --af-ink: #12181F;
  --af-shadow: none;
  --af-radius: 6px;
  --af-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, monospace;
}

html, body, [class*="st-"] { font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif; }
.stApp { background: var(--af-canvas); color: var(--af-body); }
.block-container { padding-top: 0.6rem; padding-bottom: 3.5rem; max-width: 1280px; }

/* Chrome: drop the menu/footer, keep sidebar + header. */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { right: 1rem; }

h1, h2, h3 { color: var(--af-heading); font-weight: 700; letter-spacing: -0.012em; }
h1 { font-size: 1.75rem; }
h2 { font-size: 1.3rem; margin-top: 0.4rem; }
h3 { font-size: 1.05rem; }
p, li { color: var(--af-body); font-size: 0.92rem; line-height: 1.6; max-width: 78ch; }
[data-testid="stCaptionContainer"], .af-caption {
  color: var(--af-muted); font-size: 0.8rem; line-height: 1.55; margin: 0.35rem 0 0.9rem;
}
[data-testid="stCaptionContainer"] { margin-top: -0.2rem; }
hr, [data-testid="stMarkdownHorizontalRule"] { border-color: var(--af-border); }

a { color: var(--af-accent); }
/* Visible keyboard focus everywhere. */
button:focus-visible, a:focus-visible, [role="button"]:focus-visible,
input:focus-visible, [data-testid="stSidebarNav"] a:focus-visible {
  outline: 2px solid var(--af-accent); outline-offset: 2px;
}

/* Sidebar */
[data-testid="stSidebar"] { background: var(--af-surface); border-right: 1px solid var(--af-border); }
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a { border-radius: var(--af-radius); }
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a:hover { background: var(--af-accent-soft); }
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a[aria-current="page"] {
  background: var(--af-accent-soft); box-shadow: inset 2px 0 0 var(--af-accent);
}

/* Native metrics + form chrome */
[data-testid="stMetric"] {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); padding: 0.9rem 1rem; box-shadow: none;
}
[data-testid="stMetricValue"] {
  color: var(--af-heading); font-family: var(--af-mono); font-weight: 700;
  font-size: 1.55rem; font-variant-numeric: tabular-nums;
}
[data-testid="stMetricLabel"] { color: var(--af-muted); font-size: 0.78rem; letter-spacing: 0; }
[data-testid="stMetricDelta"] { font-weight: 600; }

.stButton > button, .stFormSubmitButton > button, [data-testid="stBaseButton-primary"] {
  border-radius: var(--af-radius); font-weight: 600; font-size: 0.88rem; padding: 0.5rem 1.15rem;
  border: 1px solid var(--af-accent); background: var(--af-accent); color: #FFFFFF;
  box-shadow: none; transition: background .15s ease, border-color .15s ease;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
  background: var(--af-accent-hover); border-color: var(--af-accent-hover); color: #FFFFFF;
}
.stButton > button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
  background: var(--af-surface); color: var(--af-heading); border: 1px solid var(--af-border);
  box-shadow: none;
}
.stButton > button[kind="secondary"]:hover { border-color: var(--af-accent); color: var(--af-accent); }

[data-testid="stExpander"] {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); box-shadow: none;
}
[data-testid="stExpander"] summary { font-weight: 600; color: var(--af-heading); }
[data-testid="stExpander"] summary:hover { color: var(--af-accent); }

[data-testid="stDataFrame"], [data-testid="stTable"] { border: 1px solid var(--af-border); border-radius: var(--af-radius); }
[data-testid="stTabs"] button[aria-selected="true"] { color: var(--af-accent); font-weight: 600; }
[data-testid="stAlert"] { border-radius: var(--af-radius); border: 1px solid var(--af-border); }

/* ---------- af- components ---------- */
/* Masthead: the one dark surface, the one shadow, the one reveal. */
.af-hero {
  background: var(--af-ink); border: none; border-radius: var(--af-radius);
  padding: 1.35rem 1.5rem 1.2rem; color: #FFFFFF; margin: 0.2rem 0 1.2rem;
  animation: af-rise 0.45s ease-out both;
}
@keyframes af-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .af-hero { animation: none; }
  * { animation-duration: 0.001s !important; transition-duration: 0.001s !important; }
}
.af-hero-kicker {
  font-family: var(--af-mono); font-size: 0.78rem; font-weight: 500;
  color: #8FA3B8; margin-bottom: 0.55rem;
}
.af-hero-kicker::before { content: "> "; color: #5B7186; }
.af-hero-title {
  font-family: var(--af-mono); font-size: 1.95rem; font-weight: 800;
  letter-spacing: -0.03em; line-height: 1.1; color: #FFFFFF;
}
.af-hero-tag {
  font-size: 0.95rem; color: #C4CFDA; margin-top: 0.55rem; max-width: 66ch;
  line-height: 1.6; font-weight: 400;
}
.af-hero-facts {
  display: flex; flex-wrap: wrap; gap: 0.35rem 1.4rem; margin-top: 1.05rem;
  padding-top: 0.8rem; border-top: 1px solid #2A3540;
  font-size: 0.8rem; font-weight: 500; color: #A9B7C6;
}
.af-hero-facts i { font-style: normal; color: #526172; padding-right: 0.45rem; }
.af-hero-pill {
  background: rgba(255, 255, 255, 0.14); border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 999px; padding: 0.3rem 0.8rem; font-size: 0.79rem; font-weight: 600; color: #FFFFFF;
}

.af-section-title { color: var(--af-heading); font-weight: 700; font-size: 1.16rem; letter-spacing: -0.01em; }
.af-section-sub { color: var(--af-muted); font-size: 0.84rem; margin-top: 0.15rem; }
.af-section-rule { border-left: 2px solid var(--af-accent); padding-left: 0.7rem; margin: 1.6rem 0 0.7rem; }

.af-metric {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); box-shadow: none; padding: 1rem 1.15rem; height: 100%;
}
.af-metric-label { font-size: 0.82rem; font-weight: 600; letter-spacing: 0;
  text-transform: none; color: var(--af-muted); }
.af-metric-value { font-family: var(--af-mono); font-size: 1.75rem; font-weight: 700;
  color: var(--af-heading); line-height: 1.2; margin-top: 0.3rem;
  letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.af-metric-delta { font-size: 0.8rem; font-weight: 600; margin-top: 0.2rem; }
.af-metric-delta--up { color: #176B4F; }
.af-metric-delta--down { color: #B42318; }
.af-metric-help { font-size: 0.75rem; color: var(--af-muted); margin-top: 0.25rem; }

/* Definition rows: title + prose separated by hairlines, no card kit. */
.af-defs { display: flex; flex-direction: column; }
.af-def { border-left: 2px solid var(--af-border); padding: 0.2rem 0 0.2rem 0.95rem; height: 100%; }
.af-def-title { color: var(--af-heading); font-weight: 700; font-size: 0.95rem; margin-bottom: 0.25rem; }
.af-def-body { color: var(--af-body); font-size: 0.86rem; line-height: 1.55; }

/* Compact mono line for machine names / captions. */
.af-term { font-family: var(--af-mono); }

.af-card {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); box-shadow: none;
  padding: 1.15rem 1.25rem; height: 100%;
}
.af-card:hover { border-color: #B8C2CC; }
.af-card-icon { font-size: 0.85rem; line-height: 1.2; margin-bottom: 0.55rem; display: inline-block;
  font-family: var(--af-mono); font-weight: 700; color: var(--af-accent); }
.af-card-title { color: var(--af-heading); font-weight: 700; font-size: 0.98rem; margin-bottom: 0.3rem; }
.af-card-body { color: var(--af-body); font-size: 0.86rem; line-height: 1.55; }

.af-badge {
  display: inline-block; border-radius: 999px; padding: 0.14rem 0.62rem;
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.01em;
  border: 1px solid #C7CFD8; background: #EEF1F4; color: #3B4552; white-space: nowrap;
}
.af-badge--draft { background: #EEF1F4; color: #3B4552; border-color: #C7CFD8; }
.af-badge--designing { background: #F3F0FE; color: #5B21B6; border-color: #DDD6FE; }
.af-badge--reviewing, .af-badge--mock { background: #FDF3E3; color: #92610A; border-color: #F2D8A6; }
.af-badge--approved { background: var(--af-accent-soft); color: #11533E; border-color: #BCD8CA; }
.af-badge--blocked { background: #FBEBE9; color: #98261B; border-color: #F0C4BE; }
.af-badge--failed { background: #F9DEDC; color: #7F1D1D; border-color: #EAB8B4; }

.af-legend { display: flex; flex-wrap: wrap; gap: 0.4rem 1.1rem; margin: 0.45rem 0 1.1rem; }
.af-legend-item { display: inline-flex; align-items: center; gap: 0.4rem;
  font-size: 0.76rem; color: var(--af-body); font-weight: 500; }
.af-legend-swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block;
  border: 1px solid rgba(18, 24, 31, 0.15); }
"""

# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #


def inject_css() -> None:
    """Inject the webfonts (Inter + JetBrains Mono) and the dashboard stylesheet.

    Call exactly once, immediately after ``st.set_page_config()``.
    """
    st.markdown(_FONT_LINKS, unsafe_allow_html=True)
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)


def af_badge(status: str, label: str | None = None) -> str:
    """Return an HTML pill badge for a campaign/agent status.

    Unknown statuses render in neutral slate instead of raising.
    """
    text = label if label is not None else str(status or "").replace("_", " ").strip().title()
    return f'<span class="af-badge af-badge--{_esc(status_class(status))}">{_esc(text)}</span>'


def af_metric(
    label: str,
    value: str,
    delta: str | None = None,
    help_text: str | None = None,
) -> None:
    """Render a figure block (hairline-bordered card, mono number, sentence-case label).

    ``delta`` gets a green up-arrow when it starts with ``+`` and a red
    down-arrow when it starts with ``-``; anything else renders as neutral text.
    """
    delta_html = ""
    if delta:
        raw = str(delta).strip()
        if raw.startswith("-"):
            delta_html = f'<div class="af-metric-delta af-metric-delta--down">&#9660; {_esc(raw)}</div>'
        elif raw.startswith("+"):
            delta_html = f'<div class="af-metric-delta af-metric-delta--up">&#9650; {_esc(raw)}</div>'
        else:
            delta_html = f'<div class="af-metric-delta">{_esc(raw)}</div>'
    help_html = f'<div class="af-metric-help">{_esc(help_text)}</div>' if help_text else ""
    st.markdown(
        '<div class="af-metric">'
        f'<div class="af-metric-label">{_esc(label)}</div>'
        f'<div class="af-metric-value">{_esc(value)}</div>'
        f"{delta_html}{help_html}"
        "</div>",
        unsafe_allow_html=True,
    )


def af_section(title: str, subtitle: str | None = None) -> None:
    """Render a section header marked by a quiet pine rule."""
    sub_html = f'<div class="af-section-sub">{_esc(subtitle)}</div>' if subtitle else ""
    st.markdown(
        '<div class="af-section-rule">'
        f'<div class="af-section-title">{_esc(title)}</div>{sub_html}'
        "</div>",
        unsafe_allow_html=True,
    )


def af_card(icon: str, title: str, body: str) -> str:
    """Return an HTML card. ``icon`` renders as a small mono term (or "" for none)."""
    icon_html = f'<span class="af-card-icon">{_esc(icon)}</span>' if icon else ""
    return (
        '<div class="af-card">'
        f"{icon_html}"
        f'<div class="af-card-title">{_esc(title)}</div>'
        f'<div class="af-card-body">{_esc(body)}</div>'
        "</div>"
    )


def af_legend(items: list[tuple[str, str]]) -> None:
    """Render a colour legend: ``[("Orchestration", "#176B4F"), ...]``."""
    swatches = "".join(
        '<span class="af-legend-item">'
        f'<span class="af-legend-swatch" style="background:{_esc(color)}"></span>'
        f"{_esc(name)}</span>"
        for name, color in items
    )
    st.markdown(f'<div class="af-legend">{swatches}</div>', unsafe_allow_html=True)


def af_caption(text: str) -> None:
    """Render muted explanatory copy under a diagram or chart."""
    st.markdown(f'<div class="af-caption">{_esc(text)}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Mermaid
# --------------------------------------------------------------------------- #
_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"
_FONT_AWESOME_CDN = (
    "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.5.2/css/all.min.css"
)

_MERMAID_INIT = """
mermaid.initialize({
  startOnLoad: false,
  securityLevel: 'loose',
  theme: 'base',
  fontFamily: 'Inter, sans-serif',
  flowchart: { htmlLabels: true, curve: 'basis', padding: 12, nodeSpacing: 48, rankSpacing: 58 },
  themeVariables: {
    primaryColor: '#E9F2ED',
    primaryTextColor: '#12181F',
    primaryBorderColor: '#176B4F',
    lineColor: '#8A94A0',
    fontSize: '13px',
    fontFamily: 'Inter, sans-serif'
  },
  themeCSS: '.node rect,.node polygon,.node circle{filter:none;} .nodeLabel{font-weight:500;} .edgeLabel{background:#F3F5F7;color:#3B4552;font-size:11px;} .cluster rect{fill:#FFFFFF;stroke:#D9DFE5;rx:6;stroke-width:1px;} .cluster text{fill:#12181F;font-weight:700;}'
});
"""


def render_mermaid(code: str, height: int) -> None:
    """Render a Mermaid diagram in a standalone HTML iframe.

    ``code`` is the diagram source and is embedded with ``json.dumps`` so the
    Streamlit component script can never be broken by quotes or newlines in the
    label text. A syntax error surfaces as readable text inside the iframe
    instead of a blank box.
    """
    doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2"
        "?family=Inter:wght@400;500;600;700"
        "&family=JetBrains+Mono:wght@500;700;800&display=swap'>"
        f"<link rel='stylesheet' href='{_FONT_AWESOME_CDN}'>"
        f"<script src='{_MERMAID_CDN}'></script>"
        "<style>body{margin:0;padding:8px;background:transparent;"
        "font-family:Inter,sans-serif;}"
        "pre.err{color:#98261B;font-size:12px;white-space:pre-wrap;}</style>"
        "</head><body><div id='diagram'></div><script>"
        + _MERMAID_INIT
        + "const src = "
        + json.dumps(code)
        + ";mermaid.render('adforgeDiagram', src)"
        ".then(function(res){document.getElementById('diagram').innerHTML = res.svg;})"
        ".catch(function(err){document.getElementById('diagram').innerHTML ="
        " '<pre class=\"err\">' + err + '</pre>';});"
        "</script></body></html>"
    )
    st.components.v1.html(doc, height=height, scrolling=True)
