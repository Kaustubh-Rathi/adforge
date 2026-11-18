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
        af_empty,
        af_legend,
        af_metric,
        af_note,
        af_section,
        af_table,
        af_verdict,
        inject_css,
        render_mermaid,
    )

    inject_css()          # first call, right after st.set_page_config()
    af_note("Simulated KPI rows: the ROI arithmetic is real, the inputs are not.")
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

html, body { font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif; }
/* Streamlit renders icons as Material Symbols ligatures (e.g. the literal text
   "keyboard_double_arrow_right"). A blanket [class*="st-"] font rule catches
   those spans too and turns every icon into its raw ligature name, so scope the
   body font to text containers only and let the icon font win. */
.stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
[data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"] {
  font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
}
[data-testid="stIconMaterial"], .material-symbols-rounded, .material-icons {
  font-family: 'Material Symbols Rounded', 'Material Icons' !important;
}
.stApp { background: var(--af-canvas); color: var(--af-body); }
.block-container { padding-top: 0.6rem; padding-bottom: 3.5rem; max-width: 1280px; }

/* Chrome: drop the hosted-Streamlit chrome (menu, footer, Deploy button) so
   the console reads as a product surface, not a Streamlit Cloud demo. The
   running indicator is kept because agent runs take minutes. */
#MainMenu, footer, [data-testid="stDecoration"] { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"], [data-testid="stAppDeployButton"] { display: none !important; }

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
[data-testid="stSidebar"] {
  background: var(--af-surface); border-right: 1px solid var(--af-border); min-width: 264px;
}
[data-testid="stSidebar"] [data-testid="stSidebarNav"] { overflow-x: clip; }
/* Product mark above the nav. Legacy multipage apps have no header slot for
   it, so the nav container carries the wordmark as generated content. */
[data-testid="stSidebar"] [data-testid="stSidebarNav"]::before {
  content: "AdForge"; display: block; font-family: var(--af-mono);
  font-size: 0.95rem; font-weight: 800; letter-spacing: -0.02em;
  color: var(--af-heading); padding: 0.35rem 0.6rem 0.1rem; margin: 0 0.45rem;
}
[data-testid="stSidebar"] [data-testid="stSidebarNav"]::after {
  content: "campaign operations console"; display: block;
  font-family: var(--af-mono); font-size: 0.66rem; color: var(--af-muted);
  letter-spacing: 0.02em; padding: 0 0.6rem 0.55rem; margin: 0 0.45rem 0.35rem;
  border-bottom: 1px solid var(--af-border);
}
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a {
  border-radius: var(--af-radius); padding: 0.32rem 0.6rem;
  margin: 0.1rem 0.45rem; white-space: nowrap;
}
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a:hover { background: var(--af-accent-soft); }
[data-testid="stSidebar"] [data-testid="stSidebarNav"] a[aria-current="page"] {
  background: var(--af-accent-soft); box-shadow: inset 2px 0 0 var(--af-accent);
}
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding-top: 0.35rem; }

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

/* Streamlit wraps button labels in <p>, and the global prose colour would
   otherwise paint every primary label slate-on-pine. Make the label inherit
   the button's own colour instead. */
.stButton > button p, .stFormSubmitButton > button p,
.stDownloadButton > button p, [data-testid^="stBaseButton"] p {
  color: inherit; font-size: inherit; font-weight: inherit; line-height: 1.2;
  max-width: none; margin: 0;
}
.stButton > button:disabled, .stFormSubmitButton > button:disabled,
[data-testid^="stBaseButton"]:disabled {
  background: #E7EBEF; border-color: var(--af-border); color: var(--af-muted);
  cursor: not-allowed;
}

/* Inputs: one radius, one hairline, readable size. */
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] {
  border-radius: var(--af-radius);
}
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea { font-size: 0.9rem; }

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

/* Inline note strip: the replacement for st.info / st.warning on surfaces
   where Streamlit's own alert palette would break the token language. */
.af-note {
  border-left: 3px solid var(--af-accent); background: var(--af-accent-soft);
  color: #11533E; border-radius: 0 var(--af-radius) var(--af-radius) 0;
  padding: 0.7rem 0.95rem; font-size: 0.86rem; line-height: 1.55;
  margin: 0.55rem 0 0.9rem; max-width: 88ch;
}
.af-note--warn { border-left-color: #B45309; background: #FDF3E3; color: #7C4A08; }
.af-note--bad { border-left-color: #B42318; background: #FBEBE9; color: #7F1D1D; }
.af-note b { color: inherit; }

/* Empty state: a deliberate "nothing here yet" panel instead of a blue alert
   or a lone monospace zero. */
.af-empty {
  border: 1px dashed #C7CFD8; border-radius: var(--af-radius);
  background: #FBFCFD; padding: 1.35rem 1.5rem; margin: 0.4rem 0 1rem;
}
.af-empty-kicker {
  font-family: var(--af-mono); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--af-muted);
}
.af-empty-title { color: var(--af-heading); font-weight: 700; font-size: 1rem; margin-top: 0.4rem; }
.af-empty-body { color: var(--af-body); font-size: 0.88rem; line-height: 1.55;
  margin-top: 0.3rem; max-width: 72ch; }
.af-empty-hint { margin-top: 0.65rem; font-family: var(--af-mono); font-size: 0.78rem;
  color: var(--af-accent); }

/* Data tables: hairline ledger, sticky header, mono figures. */
.af-table-wrap {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); overflow: auto; margin-top: 0.4rem;
  max-width: 100%;
}
table.af-table { width: 100%; min-width: 620px; border-collapse: collapse; font-size: 0.85rem; }
table.af-table th {
  text-align: left; font-weight: 600; color: var(--af-heading);
  font-size: 0.76rem; padding: 0.6rem 1rem; background: var(--af-canvas);
  border-bottom: 1px solid var(--af-border); position: sticky; top: 0; z-index: 1;
  white-space: nowrap;
}
table.af-table td {
  padding: 0.55rem 1rem; border-bottom: 1px solid var(--af-border);
  color: var(--af-body); font-variant-numeric: tabular-nums; vertical-align: top;
}
table.af-table tr:last-child td { border-bottom: none; }
table.af-table tbody tr:hover td { background: #FAFBFC; }
table.af-table td.af-agent { font-weight: 600; color: var(--af-heading); font-family: var(--af-mono); }
table.af-table .af-num { text-align: right; }
table.af-table td.af-mono { font-family: var(--af-mono); }

/* Campaign ledger rows: name/status left, money right, colour stripe by status. */
.af-ops-list { display: flex; flex-direction: column; gap: 0.55rem; margin-top: 0.3rem; }
.af-ops-row {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center;
  gap: 1.25rem; background: var(--af-surface); border: 1px solid var(--af-border);
  border-left: 3px solid var(--af-accent); border-radius: var(--af-radius);
  box-shadow: var(--af-shadow); padding: 0.75rem 1.1rem;
}
.af-ops-name { font-weight: 600; color: var(--af-heading); font-size: 0.92rem; }
.af-ops-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 0.6rem; margin-top: 0.3rem; }
.af-ops-id { color: var(--af-muted); font-size: 0.74rem; font-family: var(--af-mono); }
.af-ops-link { color: var(--af-accent); font-size: 0.76rem; font-weight: 600; text-decoration: none; }
.af-ops-link:hover { text-decoration: underline; }
.af-ops-figures { display: flex; gap: 1.7rem; text-align: right; }
.af-ops-fig-label { font-size: 0.72rem; font-weight: 600; color: var(--af-muted); }
.af-ops-fig-value {
  font-size: 0.98rem; font-weight: 700; color: var(--af-heading);
  font-variant-numeric: tabular-nums; margin-top: 0.1rem; font-family: var(--af-mono);
}

/* Review dossiers: label/value documents, not card soup. */
.af-doc {
  background: var(--af-surface); border: 1px solid var(--af-border);
  border-radius: var(--af-radius); box-shadow: var(--af-shadow);
  padding: 1.1rem 1.3rem; margin-bottom: 0.9rem;
}
.af-doc-head {
  display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;
  border-bottom: 1px solid var(--af-border); padding-bottom: 0.55rem; margin-bottom: 0.75rem;
}
.af-doc-title { font-weight: 700; color: var(--af-heading); font-size: 1rem; }
.af-doc-row {
  display: grid; grid-template-columns: 170px minmax(0, 1fr);
  gap: 1rem; padding: 0.32rem 0; font-size: 0.88rem;
}
.af-doc-label { color: var(--af-muted); font-weight: 600; font-size: 0.8rem; }
.af-doc-value { color: var(--af-body); line-height: 1.5; word-break: break-word; }
.af-doc-value strong { color: var(--af-heading); font-weight: 600; }
.af-doc-list { margin: 0.15rem 0 0 1.1rem; padding: 0; }
.af-doc-list li { font-size: 0.86rem; color: var(--af-body); margin: 0.15rem 0; }
.af-alloc-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; margin-top: 0.3rem; }
.af-alloc-table th {
  text-align: left; font-weight: 600; font-size: 0.78rem; color: var(--af-heading);
  border-bottom: 1px solid var(--af-border); padding: 0.35rem 0.6rem 0.35rem 0;
}
.af-alloc-table td {
  padding: 0.35rem 0.6rem 0.35rem 0; border-bottom: 1px solid var(--af-border);
  color: var(--af-body); font-variant-numeric: tabular-nums;
}
.af-alloc-table tr:last-child td { border-bottom: none; }
.af-alloc-table .af-right { text-align: right; }

/* Verdict banner: the one loud element on the ROI page. */
.af-verdict {
  display: grid; grid-template-columns: minmax(200px, auto) minmax(0, 1fr); gap: 2rem;
  align-items: center; background: var(--af-surface);
  border: 1px solid var(--af-border); border-left: 6px solid var(--af-accent);
  border-radius: var(--af-radius); box-shadow: var(--af-shadow);
  padding: 1.5rem 1.75rem; margin: 0.6rem 0 1.1rem;
}
.af-verdict-word { font-family: var(--af-mono); font-size: 2.5rem; font-weight: 800;
  letter-spacing: -0.03em; line-height: 1; }
.af-verdict-kicker { font-size: 0.78rem; font-weight: 600; color: var(--af-muted);
  margin-bottom: 0.45rem; }
.af-verdict-stats { display: grid; grid-template-columns: repeat(4, minmax(0, auto)); gap: 1.8rem; }
.af-verdict-stat-label { font-size: 0.74rem; font-weight: 600; color: var(--af-muted); }
.af-verdict-stat-value { font-family: var(--af-mono); font-size: 1.25rem; font-weight: 700;
  color: var(--af-heading); font-variant-numeric: tabular-nums; margin-top: 0.15rem;
  letter-spacing: -0.01em; }
.af-verdict-why { grid-column: 1 / -1; font-size: 0.88rem; color: var(--af-body);
  line-height: 1.55; border-top: 1px solid var(--af-border); padding-top: 0.8rem;
  max-width: 88ch; }
.af-verdict-why b { color: var(--af-heading); }

/* Narrow viewports: grids collapse before Streamlit stacks its columns. */
@media (max-width: 900px) {
  .af-verdict { grid-template-columns: 1fr; gap: 1rem; }
  .af-verdict-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
  .af-ops-row { grid-template-columns: 1fr; }
  .af-ops-figures { justify-content: flex-start; text-align: left; }
  .af-doc-row { grid-template-columns: 1fr; gap: 0.15rem; }
  /* Four Streamlit columns still sit side by side at this width; the mono
     figures must shrink or "$10,901.67" breaks across two lines. */
  .af-metric { padding: 0.8rem 0.9rem; }
  .af-metric-value { font-size: 1.3rem; }
}
@media (max-width: 640px) {
  .af-hero { padding: 1.1rem 1.15rem; }
  .af-hero-title { font-size: 1.5rem; }
  .af-metric-value { font-size: 1.45rem; }
  .af-verdict-word { font-size: 2rem; }
  .af-verdict { padding: 1.1rem 1.15rem; }
}
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


def af_note(text: str, tone: str = "info") -> None:
    """Render a tinted note strip instead of a Streamlit alert.

    ``text`` may contain simple inline HTML (``<b>``, ``<code>``); ``tone`` is
    ``info`` (pine), ``warn`` (amber) or ``bad`` (red).
    """
    modifier = {"info": "", "warn": " af-note--warn", "bad": " af-note--bad"}.get(tone, "")
    st.markdown(f'<div class="af-note{modifier}">{text}</div>', unsafe_allow_html=True)


def af_empty(
    title: str,
    body: str,
    hint: str | None = None,
    kicker: str = "Nothing here yet",
) -> None:
    """Render a deliberate empty-state panel.

    Replaces ``st.info`` for "no campaigns / no runs / queue clear" so an empty
    screen still looks designed instead of showing a browser-blue callout.
    """
    hint_html = f'<div class="af-empty-hint">{_esc(hint)}</div>' if hint else ""
    st.markdown(
        '<div class="af-empty">'
        f'<div class="af-empty-kicker">{_esc(kicker)}</div>'
        f'<div class="af-empty-title">{_esc(title)}</div>'
        f'<div class="af-empty-body">{_esc(body)}</div>'
        f"{hint_html}</div>",
        unsafe_allow_html=True,
    )


def af_table(
    headers: list[str],
    rows: list[list[str]],
    numeric_cols: set[int] | None = None,
    min_width: int = 620,
) -> str:
    """Return a ledger table fragment.

    ``rows`` cells are trusted HTML (they usually embed :func:`af_badge`);
    header labels are escaped. ``numeric_cols`` right-aligns both the header
    and the cells at those indexes.
    """
    right = numeric_cols or set()
    head = "".join(
        f'<th class="af-num">{_esc(h)}</th>' if i in right else f"<th>{_esc(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="af-num">{cell}</td>' if i in right else f"<td>{cell}</td>"
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="af-table-wrap" style="max-width:100%">'
        f'<table class="af-table" style="min-width:{int(min_width)}px">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def af_verdict(
    word: str,
    tone: str,
    stats: list[tuple[str, str]],
    note: str | None = None,
    note_label: str = "Agent rationale",
) -> None:
    """Render the ROI verdict banner.

    ``tone`` is a status token (``approved`` / ``reviewing`` / ``blocked`` /
    ``failed``); ``stats`` is an ordered ``[(label, value), ...]`` list;
    ``note`` is optional prose shown under a rule. ``word`` and the figures are
    escaped, ``note_label`` supplies the bold lead-in for the note.
    """
    colour = status_color(tone)
    stats_html = "".join(
        f'<div><div class="af-verdict-stat-label">{_esc(label)}</div>'
        f'<div class="af-verdict-stat-value">{_esc(value)}</div></div>'
        for label, value in stats
    )
    note_html = (
        f'<div class="af-verdict-why"><b>{_esc(note_label)}</b> {_esc(note)}</div>'
        if note
        else ""
    )
    st.markdown(
        f'<div class="af-verdict" style="border-left-color:{colour}">'
        f'<div><div class="af-verdict-kicker">Recommended action</div>'
        f'<div class="af-verdict-word" style="color:{colour}">{_esc(word)}</div></div>'
        f'<div class="af-verdict-stats">{stats_html}</div>'
        f"{note_html}</div>",
        unsafe_allow_html=True,
    )


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
    """Render a Mermaid diagram scaled to fit its iframe.

    ``code`` is the diagram source and is embedded with ``json.dumps`` so the
    Streamlit component script can never be broken by quotes or newlines in the
    label text. Once Mermaid has produced the SVG the script drops its intrinsic
    width/height and sets ``width/height: 100%`` with
    ``preserveAspectRatio="xMidYMid meet"``, so the whole diagram is always
    visible and centred at the largest legible size the frame allows. A syntax
    error surfaces as readable text inside the iframe instead of a blank box.
    """
    doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2"
        "?family=Inter:wght@400;500;600;700"
        "&family=JetBrains+Mono:wght@500;700;800&display=swap'>"
        f"<link rel='stylesheet' href='{_FONT_AWESOME_CDN}'>"
        f"<script src='{_MERMAID_CDN}'></script>"
        "<style>html,body{height:100%;margin:0;padding:0;overflow:hidden;"
        "background:transparent;font-family:Inter,sans-serif;}"
        "#diagram{height:100%;width:100%;display:flex;align-items:center;"
        "justify-content:center;padding:8px;box-sizing:border-box;}"
        "#diagram svg{display:block;width:100%;height:100%;max-width:100%;max-height:100%;}"
        "#loading{position:absolute;inset:0;display:flex;align-items:center;"
        "justify-content:center;font-family:'JetBrains Mono',monospace;"
        "font-size:11px;color:#6B7683;}"
        "pre.err{color:#98261B;font-size:12px;white-space:pre-wrap;margin:8px;}"
        "</style>"
        "</head><body><div id='diagram'></div>"
        "<div id='loading'>rendering diagram</div><script>"
        + _MERMAID_INIT
        + "const src = "
        + json.dumps(code)
        + ";mermaid.render('adforgeDiagram', src)"
        ".then(function(res){"
        "document.getElementById('loading').remove();"
        "const host = document.getElementById('diagram');"
        "host.innerHTML = res.svg;"
        "const svg = host.querySelector('svg');"
        "if (svg) { svg.removeAttribute('height'); svg.setAttribute('width','100%');"
        "svg.setAttribute('height','100%'); svg.style.maxWidth='100%';"
        "svg.style.maxHeight='100%'; svg.setAttribute('preserveAspectRatio','xMidYMid meet'); }"
        "})"
        ".catch(function(err){"
        "const loading = document.getElementById('loading'); if (loading) loading.remove();"
        "const host = document.getElementById('diagram');"
        "host.setAttribute('data-af-diagram-error','1');"
        "host.innerHTML = '<pre class=\"err\">' + err + '</pre>';});"
        "</script></body></html>"
    )
    st.components.v1.html(doc, height=height, scrolling=False)
