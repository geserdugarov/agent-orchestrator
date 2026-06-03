# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Visual theme tokens shared by the dashboard charts and chrome.

This module exports plain data -- colors, font / size tokens, and the
plotly layout defaults assembled from them. It deliberately does NOT
import plotly: keeping the tokens dependency-free lets
`orchestrator/dashboard.py` consult them at module load (e.g. to pick
a header color for a Streamlit banner) without dragging the optional
`dashboard` group into the polling tick's import surface. The actual
plotly figure builders live in `orchestrator/dashboard_charts.py`,
which is allowed to import plotly because it is imported lazily from
`dashboard.main()` (see the lazy-import guard in
`tests/test_dashboard.py`).

The categorical palettes (`EVENT_COLORS`, `STAGE_COLORS`,
`COST_SOURCE_COLORS`, `TOKEN_TYPE_COLORS`, `BACKEND_COLORS`,
`REVIEW_ROUND_COLORS`) map the dimension values the dashboard renders
to stable colors so the same dimension keeps the same hue on every
chart and across sessions. Values not in the explicit map fall through
to `CATEGORICAL_PALETTE`, an ordered sequence used by `color_for(...)`
as a deterministic backup.

The palette mirrors the standalone redesigned analytics mock (issue
#341): a warm cream background, an accent purple, and per-token-type /
per-backend hues that read against that background. `.streamlit/config.toml`
also pulls from this palette so Plotly figures sit cleanly inside the
surrounding Streamlit chrome instead of clashing with it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# Page chrome. Mirrors the standalone mock's :root tokens verbatim --
# a cool gray page (#f4f5f8) with white cards, indigo accent, and the
# IBM Plex Sans / Mono pair. The reviewer flagged an earlier warm-cream
# palette as a visual mismatch against the attached reference; these
# values now come straight off the mock's `:root` block.
BACKGROUND = "#f4f5f8"
CARD_BG = "#ffffff"
SURFACE = "#f0f1f6"        # mock --chip-bg
TEXT = "#1c2030"           # mock --ink
MUTED_TEXT = "#565d72"     # mock --ink-2
MUTED_TEXT_SOFT = "#8a90a3"  # mock --ink-3
GRID = "#eef0f5"           # mock --grid
BORDER = "#e6e8ef"         # mock --border

# Brand / semantic colors used by KPI deltas and insight banners.
ACCENT = "#5b54e0"
PRIMARY = ACCENT
SECONDARY = "#8b5cf6"
SUCCESS = "#2f9e6b"        # mock --pos
WARNING = "#e0913a"
DANGER = "#d9534a"         # mock --neg
NEUTRAL = "#6b7280"
INK = TEXT

# Geometry tokens lifted from the mock's `:root` block. The redesigned
# cards use a 14px radius (not 10px), 20px padding, and 16px gaps; the
# content column stretches to 1480px before the page chrome lets it
# breathe -- an earlier draft used 10px / 14px / 1240px and the
# reviewer called out the mismatch.
RADIUS = "14px"
CARD_PADDING = "20px"
GRID_GAP = "16px"
CONTENT_MAX_WIDTH = "1480px"
# The sticky top bar's resting height -- the filter bar's `top:` sits
# one pixel below it so the two share a single border line when the
# operator scrolls.
TOPBAR_STICKY_HEIGHT = "71px"

FONT_FAMILY = (
    '"IBM Plex Sans", -apple-system, BlinkMacSystemFont, '
    '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
)
MONO_FONT_FAMILY = (
    '"IBM Plex Mono", ui-monospace, SFMono-Regular, "SF Mono", '
    'Menlo, Consolas, "Liberation Mono", monospace'
)
FONT_SIZE = 13
TITLE_FONT_SIZE = 15

# Token-type segments for the hero spend & token usage chart. The
# three hues are tuned to read against the cool gray page background
# and stack in the order Input / Output / Cache from bottom to top.
TOKEN_TYPE_COLORS: Mapping[str, str] = {
    "Input": "#5b6cf0",
    "Output": "#e0913a",
    "Cache": "#1aa39a",
}

# Agent backends. `claude` is the developer / implementer; `codex` is
# the reviewer. Keys match the strings `workflow._run_agent_tracked`
# writes to `backend`. `unknown` covers NULL rows from the read model.
BACKEND_COLORS: Mapping[str, str] = {
    "claude": ACCENT,
    "codex": "#e0913a",
    "unknown": NEUTRAL,
}

# Review-round buckets, in the order the chart renders them: the
# `0` bucket is the initial pass; everything past it is rework.
REVIEW_ROUND_COLORS: Mapping[str, str] = {
    "0": "#5b6cf0",
    "1": "#e8a13a",
    "2": "#e07a3a",
    "3-5": "#d9534a",
    "6+": "#a8201e",
    "unknown": NEUTRAL,
}

# Deterministic fallback palette for dimensions without an explicit
# mapping. Order is significant -- `color_for("foo", ["foo", "bar"])`
# returns the n-th entry for the n-th distinct value, so two charts
# rendering the same domain in the same order produce the same colors.
CATEGORICAL_PALETTE: tuple[str, ...] = (
    ACCENT,
    "#5b6cf0",
    "#e0913a",
    "#1aa39a",
    "#8b5cf6",
    "#d9534a",
    "#d98a3a",
    "#6b7a99",
    "#0ea5e9",
    "#65a30d",
)

# Analytics event kinds written by `orchestrator.analytics.append_record`.
EVENT_COLORS: Mapping[str, str] = {
    "stage_enter": ACCENT,
    "stage_evaluation": NEUTRAL,
    "agent_exit": SUCCESS,
}

# Workflow stage labels. Mirror the labels carried on live GitHub
# issues; renaming any one of them would also have to update the state
# machine, so the mapping is a public contract.
STAGE_COLORS: Mapping[str, str] = {
    "decomposing": "#8b5cf6",
    "blocked": NEUTRAL,
    "ready": "#5b6cf0",
    "umbrella": SECONDARY,
    "implementing": "#5b6cf0",
    "validating": "#e0913a",
    "documenting": "#1aa39a",
    "in_review": "#7c3aed",
    "fixing": "#d9534a",
    "resolving_conflict": "#d98a3a",
    "question": "#6b7a99",
    "done": SUCCESS,
    "rejected": NEUTRAL,
}

# `cost_source` values from `orchestrator.usage.UsageMetrics`.
COST_SOURCE_COLORS: Mapping[str, str] = {
    "reported": SUCCESS,
    "estimated": WARNING,
    "unknown-price": DANGER,
    "unknown": NEUTRAL,
    "no-usage": NEUTRAL,
}


def color_for(
    key: str,
    domain: Optional[Sequence[str]] = None,
    *,
    explicit: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve `key` to a hex color string.

    Lookup order:

    1. `explicit` (caller-supplied override, typically one of the
       module-level palettes such as `STAGE_COLORS`).
    2. Position of `key` inside `domain` if both are provided -- the
       n-th distinct value gets the n-th entry of
       `CATEGORICAL_PALETTE`, wrapping when `len(domain)` exceeds the
       palette length.
    3. Hash-based fallback so a single key still gets a stable color
       without a domain. The hash fallback uses Python's stable
       `hash(...)` modulus against the palette length; this is fine
       for visual stability *within* a process but not across processes
       (Python salts the hash). Callers that need cross-process
       stability should always pass `domain`.
    """
    if explicit is not None and key in explicit:
        return explicit[key]
    if domain is not None:
        try:
            idx = list(domain).index(key)
        except ValueError:
            idx = None
        if idx is not None:
            return CATEGORICAL_PALETTE[idx % len(CATEGORICAL_PALETTE)]
    return CATEGORICAL_PALETTE[hash(key) % len(CATEGORICAL_PALETTE)]


def base_layout(title: Optional[str] = None) -> dict[str, Any]:
    """Return the shared Plotly `layout` dict for a chart.

    The result is a plain dict -- no plotly import required. Chart
    builders in `dashboard_charts` merge it into their `Figure` via
    `fig.update_layout(**base_layout(title=...))` so every chart
    shares the same margins, font, gridlines, and background colors.
    The plot background matches `CARD_BG` (white) rather than `BACKGROUND`
    because every chart lives inside a card.
    """
    layout: dict[str, Any] = {
        "paper_bgcolor": CARD_BG,
        "plot_bgcolor": CARD_BG,
        "font": {
            "family": FONT_FAMILY,
            "size": FONT_SIZE,
            "color": TEXT,
        },
        "margin": {"l": 56, "r": 24, "t": 32 if title else 16, "b": 40},
        "legend": {
            "bgcolor": CARD_BG,
            "bordercolor": GRID,
            "borderwidth": 0,
        },
        "xaxis": {
            "gridcolor": GRID,
            "linecolor": GRID,
            "zerolinecolor": GRID,
            "tickfont": {"color": MUTED_TEXT},
        },
        "yaxis": {
            "gridcolor": GRID,
            "linecolor": GRID,
            "zerolinecolor": GRID,
            "tickfont": {"color": MUTED_TEXT},
        },
    }
    if title:
        layout["title"] = {
            "text": title,
            "font": {
                "family": FONT_FAMILY,
                "size": TITLE_FONT_SIZE,
                "color": TEXT,
            },
        }
    return layout


# CSS for the redesigned dashboard chrome. The dashboard injects this
# once at the top of the page via `st.markdown(unsafe_allow_html=True)`
# so the topbar, filter bar, KPI strip, card grid, and insight banners
# render with the target's typography and spacing. Streamlit's own
# widgets (date inputs, the segmented button group, the toggle) are
# styled inline through their `data-testid` containers so they sit
# inside the same chrome without re-implementing the controls.
#
# Token values come straight off the standalone mock's `:root` block
# (cool gray page, 14px radii, 20px padding, 16px gap, 1480px content
# max-width, IBM Plex Sans / Mono) -- the reviewer pinned a visual
# mismatch between an earlier warm-cream rewrite and the reference, so
# the values now mirror the mock 1:1.
PAGE_CSS = f"""
<style>
  :root {{
    --orch-bg: {BACKGROUND};
    --orch-card: {CARD_BG};
    --orch-ink: {INK};
    --orch-muted: {MUTED_TEXT};
    --orch-muted-soft: {MUTED_TEXT_SOFT};
    --orch-border: {BORDER};
    --orch-grid: {GRID};
    --orch-chip: {SURFACE};
    --orch-accent: {ACCENT};
    --orch-success: {SUCCESS};
    --orch-warn: {WARNING};
    --orch-danger: {DANGER};
    --orch-input: {TOKEN_TYPE_COLORS['Input']};
    --orch-output: {TOKEN_TYPE_COLORS['Output']};
    --orch-cache: {TOKEN_TYPE_COLORS['Cache']};
    --orch-radius: {RADIUS};
    --orch-pad: {CARD_PADDING};
    --orch-gap: {GRID_GAP};
  }}
  /* Page chrome -------------------------------------------------- */
  div[data-testid="stAppViewContainer"] {{
    background: var(--orch-bg);
    color: var(--orch-ink);
    font-family: {FONT_FAMILY};
  }}
  div[data-testid="stHeader"] {{ background: transparent; }}
  div[data-testid="stMain"] > div.block-container {{
    background: transparent;
    padding-top: 0; padding-bottom: 48px;
    max-width: {CONTENT_MAX_WIDTH};
  }}
  /* Topbar ------------------------------------------------------ */
  /* Sticky to top:0 within the block-container; the negative
     horizontal margins + `100vw` width let it extend past the
     content column so the cream background never leaks behind the
     bar at large viewports. */
  .orch-topbar {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 24px; flex-wrap: wrap;
    background: var(--orch-card);
    border-bottom: 1px solid var(--orch-border);
    position: sticky; top: 0; z-index: 20;
    margin: 0 calc(50% - 50vw) 0; width: 100vw;
    padding: 18px clamp(16px, 4vw, 40px);
    font-family: {FONT_FAMILY};
  }}
  .orch-brand {{ display: flex; align-items: center; gap: 14px; }}
  .orch-brand-mark {{
    width: 34px; height: 34px; border-radius: 9px;
    background: var(--orch-accent);
    display: inline-flex; align-items: center; justify-content: center;
    color: #fff; font-weight: 700; font-size: 16px;
    letter-spacing: 0.04em; flex: none;
  }}
  .orch-brand h1 {{
    margin: 0; font-size: 18px; font-weight: 600;
    color: var(--orch-ink); letter-spacing: -0.01em;
  }}
  .orch-brand .orch-sub {{
    margin: 2px 0 0; color: var(--orch-muted-soft);
    font-size: 12px; font-family: {MONO_FONT_FAMILY};
  }}
  .orch-spend {{
    display: flex; flex-direction: column; align-items: flex-end;
    gap: 2px;
  }}
  .orch-spend .label {{
    color: var(--orch-muted-soft); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.06em;
  }}
  .orch-spend .value {{
    color: var(--orch-ink); font-size: 22px; font-weight: 600;
    letter-spacing: -0.01em;
    font-family: {MONO_FONT_FAMILY};
  }}
  /* Filter bar: sticky right below the topbar so the date controls
     stay glued to the chrome as the operator scrolls -- matches the
     mock's `.filterbar2` (top: 71px). The bordered container the
     date inputs live in picks up the same full-bleed treatment via
     the `.orch-filterbar-anchor` sibling selector. */
  .orch-filterbar,
  div[data-testid="stMarkdown"]:has(.orch-filterbar-anchor)
    + div[data-testid="stVerticalBlockBorderWrapper"] {{
    background: var(--orch-card) !important;
    border: 0 !important;
    border-bottom: 1px solid var(--orch-border) !important;
    border-radius: 0 !important;
    position: sticky; top: {TOPBAR_STICKY_HEIGHT}; z-index: 19;
    margin: 0 calc(50% - 50vw) var(--orch-gap) !important;
    width: 100vw;
    padding: 11px clamp(16px, 4vw, 40px) !important;
    font-family: {FONT_FAMILY};
  }}
  div[data-testid="stMarkdown"]:has(.orch-filterbar-anchor)
    + div[data-testid="stVerticalBlockBorderWrapper"]
    div[data-testid="stHorizontalBlock"] {{
    align-items: center;
  }}
  .orch-filter-label {{
    color: var(--orch-muted-soft); font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.06em;
  }}
  .orch-filter-meta {{
    margin-left: auto; color: var(--orch-muted-soft);
    font-size: 11.5px;
    font-family: {MONO_FONT_FAMILY};
  }}
  /* Content gutter: re-add the horizontal padding the block-
     container used to provide so the cards do not sit flush
     against the page edge while the sticky bars still go
     full-bleed. */
  div[data-testid="stMain"] > div.block-container {{
    padding-left: clamp(16px, 3vw, 28px);
    padding-right: clamp(16px, 3vw, 28px);
  }}
  /* KPI strip ---------------------------------------------------- */
  .orch-kpis {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: var(--orch-gap); margin: 0 0 var(--orch-gap);
  }}
  .orch-kpi {{
    background: var(--orch-card); border: 1px solid var(--orch-border);
    border-radius: var(--orch-radius); padding: var(--orch-pad);
    display: flex; flex-direction: column;
    font-family: {FONT_FAMILY};
  }}
  .orch-kpi .kpi-top {{
    display: flex; align-items: center; justify-content: space-between;
  }}
  .orch-kpi .kpi-label {{
    color: var(--orch-muted); font-size: 12.5px; font-weight: 500;
  }}
  .orch-kpi .kpi-value {{
    color: var(--orch-ink); font-size: 30px; font-weight: 600;
    letter-spacing: -0.02em; margin: 8px 0 4px;
    font-variant-numeric: tabular-nums;
    font-family: {MONO_FONT_FAMILY}; line-height: 1.1;
  }}
  .orch-kpi .kpi-foot {{
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 10px; min-height: 30px;
    color: var(--orch-muted-soft); font-size: 11.5px;
    font-family: {MONO_FONT_FAMILY};
  }}
  .orch-delta {{
    font-size: 12px; font-weight: 600;
    padding: 2px 7px; border-radius: 6px; white-space: nowrap;
    font-family: {MONO_FONT_FAMILY};
  }}
  .orch-delta.up {{ background: rgba(217,83,74,.10);
    color: var(--orch-danger); }}
  .orch-delta.down {{ background: rgba(47,158,107,.12);
    color: var(--orch-success); }}
  .orch-delta.flat {{ background: var(--orch-chip);
    color: var(--orch-muted-soft); }}
  /* Insights banner: two-column grid (matches the mock) collapsing
     to one column under 1080px. */
  .orch-insights {{
    display: grid; grid-template-columns: 1fr 1fr;
    gap: var(--orch-gap); margin: 0 0 var(--orch-gap);
  }}
  .orch-insight {{
    display: flex; gap: 12px; align-items: flex-start;
    background: var(--orch-card); border: 1px solid var(--orch-border);
    border-radius: 12px; padding: 14px 16px;
    color: var(--orch-ink); font-size: 13.5px; line-height: 1.5;
    font-family: {FONT_FAMILY};
  }}
  .orch-insight .icon {{
    width: 22px; height: 22px; border-radius: 50%;
    background: var(--orch-ink); color: var(--orch-card);
    display: grid; place-items: center;
    font-weight: 700; font-size: 13px; flex: none; margin-top: 1px;
  }}
  .orch-insight.warning,
  .orch-insight.error {{
    background: rgba(217,83,74,.06);
    border-color: rgba(217,83,74,.22);
  }}
  .orch-insight.warning .icon,
  .orch-insight.error .icon {{
    background: var(--orch-danger); color: #fff;
  }}
  .orch-insight strong {{ font-weight: 600; margin-right: 4px; }}
  @media (max-width: 1080px) {{
    .orch-insights {{ grid-template-columns: 1fr; }}
    .orch-kpis {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  /* Card surround for charts ------------------------------------
     The dashboard wraps each card in `st.container(border=True)` so
     the chart / dataframe widgets really do sit inside the card --
     stacking a `st.markdown(...)` opening tag, the widget, and a
     closing tag would leave each widget as a sibling in the DOM
     instead. We restyle Streamlit's bordered-container wrapper to
     match the mock's 14px radius / 20px padding card. The selector
     is double-scoped through `data-testid="stMain"` so the sidebar
     bordered containers (if any) keep their default Streamlit look. */
  div[data-testid="stMain"] div[data-testid="stVerticalBlockBorderWrapper"] {{
    background: var(--orch-card) !important;
    border: 1px solid var(--orch-border) !important;
    border-radius: var(--orch-radius) !important;
    padding: var(--orch-pad) !important;
    margin-bottom: var(--orch-gap) !important;
    font-family: {FONT_FAMILY};
  }}
  /* Drop the nested border that Streamlit applies when a bordered
     container is itself inside a column -- avoids a double border. */
  div[data-testid="stMain"] div[data-testid="column"]
    div[data-testid="stVerticalBlockBorderWrapper"] {{
    margin-bottom: 0 !important;
  }}
  .orch-card-title {{
    color: var(--orch-ink); font-size: 15px; font-weight: 600;
    margin: 0; letter-spacing: -0.01em;
  }}
  .orch-card-sub {{
    color: var(--orch-muted-soft); font-size: 12px;
    margin: 3px 0 14px;
  }}
  /* Reliability tiles ------------------------------------------- */
  .orch-rel-tiles {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 9px;
    margin-bottom: 14px;
  }}
  .orch-rel-tile {{
    border: 1px solid var(--orch-border);
    border-radius: 10px; padding: 12px; text-align: center;
    background: transparent;
  }}
  .orch-rel-tile.good {{
    background: rgba(47,158,107,.07);
    border-color: rgba(47,158,107,.20);
  }}
  .orch-rel-tile.warn {{
    background: rgba(224,145,58,.10);
    border-color: rgba(224,145,58,.22);
  }}
  .orch-rel-tile.bad {{
    background: rgba(217,83,74,.10);
    border-color: rgba(217,83,74,.24);
  }}
  .orch-rel-value {{
    color: var(--orch-ink); font-size: 22px; font-weight: 600;
    letter-spacing: -0.01em;
    font-family: {MONO_FONT_FAMILY};
  }}
  .orch-rel-label {{
    color: var(--orch-muted); font-size: 11px;
    margin-top: 2px;
  }}
  /* Coverage bar ------------------------------------------------ */
  .orch-cov-title {{
    color: var(--orch-muted); font-size: 12px; font-weight: 500;
    margin: 14px 0 8px; padding-top: 14px;
    border-top: 1px solid var(--orch-border);
  }}
  .orch-cov-bar {{
    display: flex; height: 12px; border-radius: 6px;
    overflow: hidden; background: var(--orch-grid);
  }}
  .orch-cov-bar > span {{ display: block; height: 100%; }}
  .orch-cov-legend {{
    display: flex; flex-wrap: wrap; gap: 14px; margin-top: 9px;
    color: var(--orch-muted); font-size: 11.5px;
  }}
  .orch-cov-legend .dot {{
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 6px; vertical-align: middle;
  }}
  /* Footer ------------------------------------------------------ */
  .orch-foot {{
    margin-top: 22px; font-size: 11.5px;
    color: var(--orch-muted-soft); text-align: center;
    font-family: {MONO_FONT_FAMILY};
  }}
  /* Streamlit segmented control + date inputs ------------------ */
  div[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {{
    display: none;
  }}
</style>
"""


def fmt_money(value: float) -> str:
    """Compact dollar formatter matching the standalone mock (`$1.2K`,
    `$3.4M`). Used by KPIs, axis tick labels, and per-bar value labels.
    """
    n = float(value or 0.0)
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    if n < 10:
        return f"${n:.2f}"
    return f"${n:.0f}"


def fmt_money_exact(value: float) -> str:
    """Whole-dollar formatter with thousands separators (`$12,345`)."""
    return "$" + f"{round(float(value or 0.0)):,}"


def fmt_tokens(value: float) -> str:
    """Compact token-count formatter (`1.2K`, `3.4M`, `1.2B`)."""
    n = float(value or 0.0)
    if n >= 1_000_000_000:
        decimals = 0 if n >= 10_000_000_000 else 2
        return f"{n / 1_000_000_000:.{decimals}f}B"
    if n >= 1_000_000:
        decimals = 0 if n >= 10_000_000 else 1
        return f"{n / 1_000_000:.{decimals}f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(round(n)))


def fmt_num(value: float) -> str:
    """Integer with thousands separators."""
    return f"{int(round(float(value or 0.0))):,}"
