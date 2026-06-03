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
`COST_SOURCE_COLORS`) map the dimension values the dashboard renders
to stable colors so the same `agent_exit` event keeps the same hue on
every chart and across sessions. Values not in the explicit map fall
through to `CATEGORICAL_PALETTE`, an ordered sequence used by
`color_for(...)` as a deterministic backup.

The light-mode palette here mirrors the `[theme]` block in
`.streamlit/config.toml`; keeping the two in sync means the Plotly
figures sit cleanly inside the surrounding Streamlit chrome instead of
clashing with it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# Semantic colors. These also drive `.streamlit/config.toml` so the
# Plotly figures and the Streamlit chrome share a palette.
PRIMARY = "#2563eb"
SECONDARY = "#7c3aed"
SUCCESS = "#16a34a"
WARNING = "#f59e0b"
DANGER = "#dc2626"
NEUTRAL = "#6b7280"

BACKGROUND = "#ffffff"
SURFACE = "#f3f4f6"
TEXT = "#111827"
MUTED_TEXT = "#4b5563"
GRID = "#e5e7eb"

FONT_FAMILY = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, sans-serif'
)
FONT_SIZE = 13
TITLE_FONT_SIZE = 15

# Deterministic fallback palette for dimensions without an explicit
# mapping. Order is significant -- `color_for("foo", ["foo", "bar"])`
# returns the n-th entry for the n-th distinct value, so two charts
# rendering the same domain in the same order produce the same colors.
CATEGORICAL_PALETTE: tuple[str, ...] = (
    "#2563eb",  # blue 600
    "#7c3aed",  # violet 600
    "#16a34a",  # green 600
    "#f59e0b",  # amber 500
    "#dc2626",  # red 600
    "#0ea5e9",  # sky 500
    "#14b8a6",  # teal 500
    "#a855f7",  # purple 500
    "#ea580c",  # orange 600
    "#65a30d",  # lime 600
)

# Analytics event kinds written by `orchestrator.analytics.append_record`.
# Keeping the keys aligned with the event names that hit the database
# means the chart legend label and the SQL `event` value are the same
# string -- no lookup table needed.
EVENT_COLORS: Mapping[str, str] = {
    "stage_enter": PRIMARY,
    "stage_evaluation": NEUTRAL,
    "agent_exit": SUCCESS,
}

# Workflow stage labels. These mirror the labels carried on live
# GitHub issues; renaming any one of them would also have to update
# the state machine, so the mapping is a public contract.
STAGE_COLORS: Mapping[str, str] = {
    "decomposing": "#0ea5e9",
    "blocked": NEUTRAL,
    "ready": PRIMARY,
    "umbrella": SECONDARY,
    "implementing": "#a855f7",
    "validating": "#f59e0b",
    "documenting": "#14b8a6",
    "in_review": "#7c3aed",
    "fixing": "#ea580c",
    "resolving_conflict": DANGER,
    "question": "#0891b2",
    "done": SUCCESS,
    "rejected": "#4b5563",
}

# `cost_source` values from `orchestrator.usage.UsageMetrics`. Treat
# `reported` (the agent's own number) as the "good" path, `estimated`
# as the warning path (a baked-in price table guess), and the two
# unknown variants as the "ungauged" path so the cost-coverage chart
# can convey provenance at a glance.
COST_SOURCE_COLORS: Mapping[str, str] = {
    "reported": SUCCESS,
    "estimated": WARNING,
    "unknown-price": DANGER,
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
       palette length. The wrap is deterministic, so the same chart
       rendered twice produces the same colors.
    3. Hash-based fallback so a single key (e.g. for a one-shot
       annotation) still gets a stable color without a domain.

    The hash fallback uses Python's stable `hash(...)` modulus
    against the palette length; this is fine for visual stability
    *within* a process but not across processes (Python salts the
    hash). Callers that need cross-process stability should always
    pass `domain`.
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
    """
    layout: dict[str, Any] = {
        "paper_bgcolor": BACKGROUND,
        "plot_bgcolor": BACKGROUND,
        "font": {
            "family": FONT_FAMILY,
            "size": FONT_SIZE,
            "color": TEXT,
        },
        "margin": {"l": 56, "r": 24, "t": 48 if title else 24, "b": 48},
        "legend": {
            "bgcolor": BACKGROUND,
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
