# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline-HTML card builders for the analytics dashboard.

Home of the insight / backend-efficiency / cost-coverage card family: the
per-card header, the computed-insight stack, the per-backend efficiency
cards (and their metric math), the cost-attribution coverage bar (and its
segment math), and the reliability-tile strip. `orchestrator.dashboard`
re-exports these builders under their original names and
`orchestrator.dashboard_widgets` calls them directly.

Like `dashboard_html`, these builders take read-model rows / small
dataclasses (plus, where a card needs them, the formatter callables --
or the whole `dashboard_theme` handle -- the caller passes in) and
return a string the page drops into
`st.markdown(..., unsafe_allow_html=True)`. Keeping them free of any
Streamlit / Plotly import means the polling tick's import surface never
touches them. The family is self-contained (it reuses none of the
table / sparkline primitives that live in `dashboard_html`), so nothing
is imported back from `dashboard_html` here.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Sequence

from orchestrator.analytics.read import (
    BackendEfficiencyRow,
    CostCoverageRow,
)
from orchestrator.dashboard_kpis import InsightBanner

# Tokens per million, for per-million cost ratios.
_MILLION = 1_000_000


def _card_header_html(title: str, subtitle: str = "") -> str:
    """Inline HTML for the title + subtitle at the top of a card.

    Always rendered through `st.markdown(unsafe_allow_html=True)`
    INSIDE a `st.container(border=True)` block: the card visual has to
    come from a Streamlit container so the inner chart / dataframe
    widgets sit inside it. Opening a `<div class="orch-card">` in one
    `st.markdown` and closing it in another instead leaves those widgets
    as siblings of the card in Streamlit's DOM rather than children.
    """
    sub_html = (
        f'<p class="orch-card-sub">{html.escape(subtitle)}</p>'
        if subtitle
        else ""
    )
    # The hidden `.orch-cardmark` is the per-card sentinel the white-fill
    # / equal-height rules in `dashboard_theme.PAGE_CSS` key off via
    # `:has(> stElementContainer .orch-cardmark)`. Rendering it inside the
    # header markdown keeps it the bordered container's first element.
    return (
        '<span class="orch-cardmark"></span>'
        f'<p class="orch-card-title">{html.escape(title)}</p>{sub_html}'
    )


def _insights_html(
    banners: Sequence[InsightBanner],
) -> str:
    """Render the computed-insight stack.

    The colored icon (red `✕` / `!` for warning + error, neutral `›`
    / `✓` for info + success) carries the severity, so the rendered
    message no longer leads with a redundant `Warning.` / `Info.`
    prefix -- the standalone mock leads each banner with a short
    descriptive title and lets the icon paint the severity.
    """
    icon_for = {
        "error": "✕", "warning": "!", "info": "›", "success": "✓",
    }
    rows = []
    for banner in banners:
        icon = icon_for.get(banner.severity, "›")
        rows.append(
            f'<div class="orch-insight {html.escape(banner.severity)}">'
            f'<span class="icon">{icon}</span>'
            f'<span>{html.escape(banner.message)}</span>'
            '</div>'
        )
    rows_html = "".join(rows)
    return f'<div class="orch-insights">{rows_html}</div>'


def _backend_efficiency_card_html(
    row: BackendEfficiencyRow, *, theme
) -> str:
    """Render one backend-efficiency card to inline HTML.

    A spend headline over a `$ / 1M tok` · `% cache hit` · `$ / run`
    row. The caller renders one card per backend (a separate
    `st.markdown` each, so Streamlit's inter-element gap keeps the cards
    spaced). Two accounting choices match the rest of the redesigned
    page:

    - **Token total** is `input + output + cache_read + cache_write`
      (the same volume the headline KPI reports), so the `cost / 1M
      tok` tile divides by that full total rather than raw input.
    - **Cache leverage** is `cache_read / (cache_read + input)` -- the
      share of billable input served from cache, which is the cost
      lever the operator reads off the card. A high cache hit means a
      smaller fraction of input tokens pays the model's input rate.

    Colors and formatters come from the caller's `dashboard_theme`
    handle so this module stays free of the theme import (and the
    lazy-import invariant the dashboard relies on).
    """
    metrics = _backend_efficiency_metrics(row)
    color = theme.color_for(
        row.backend, explicit=theme.BACKEND_COLORS
    )
    return (
        f'<div style="border:1px solid {theme.BORDER};'
        f'border-radius:8px;padding:10px 12px;'
        f'margin-bottom:8px">'
        f'<div style="display:flex;align-items:center;'
        f'gap:8px;margin-bottom:4px">'
        f'<span style="display:inline-block;width:10px;'
        f'height:10px;border-radius:50%;background:{color}">'
        f'</span>'
        f'<b style="color:{theme.TEXT}">'
        f'{html.escape(row.backend)}</b>'
        f'<span style="color:{theme.MUTED_TEXT};'
        f'font-size:12px;margin-left:auto">'
        f'{row.runs} runs · {theme.fmt_tokens(metrics.tokens)} tok'
        '</span>'
        '</div>'
        f'<div style="color:{theme.TEXT};font-size:20px;'
        f'font-weight:600;'
        f'font-family:{theme.MONO_FONT_FAMILY};'
        f'margin-bottom:6px">'
        f'{html.escape(theme.fmt_money_exact(row.total_cost_usd))}'
        f'<span style="color:{theme.MUTED_TEXT};'
        f'font-size:11px;margin-left:8px;'
        f'font-family:{theme.FONT_FAMILY}">'
        f'spend</span></div>'
        f'<div style="display:flex;gap:14px;font-size:12px;'
        f'color:{theme.MUTED_TEXT}">'
        f'<span>${metrics.cost_per_million:.2f} / 1M tok</span>'
        f'<span>{metrics.cache_hit_pct:.0f}% cache hit</span>'
        f'<span>${metrics.cost_per_run:.2f} / run</span>'
        '</div></div>'
    )


@dataclass(frozen=True)
class _BackendEfficiencyMetrics:
    tokens: int
    cost_per_million: float
    cost_per_run: float
    cache_hit_pct: float


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _backend_efficiency_metrics(
    row: BackendEfficiencyRow,
) -> _BackendEfficiencyMetrics:
    tokens = int(
        (row.total_input_tokens or 0)
        + (row.total_output_tokens or 0)
        + (row.total_cache_read_tokens or 0)
        + (row.total_cache_write_tokens or 0)
    )
    cache_read = int(row.total_cache_read_tokens or 0)
    cache_input_total = cache_read + int(row.total_input_tokens or 0)
    return _BackendEfficiencyMetrics(
        tokens=tokens,
        cost_per_million=_safe_ratio(row.total_cost_usd, tokens / _MILLION),
        cost_per_run=_safe_ratio(row.total_cost_usd, row.runs),
        cache_hit_pct=_safe_ratio(cache_read, cache_input_total) * 100,
    )


def _cost_coverage_weights(
    rows: Sequence[CostCoverageRow],
) -> tuple[list[int], int]:
    total_tokens = sum(int(row.total_tokens or 0) for row in rows)
    if total_tokens > 0:
        return [int(row.total_tokens or 0) for row in rows], total_tokens
    weights = [int(row.runs or 0) for row in rows]
    return weights, sum(weights) or 1


def _cost_source_color(
    cost_source: str, cost_sources: Sequence[str], theme
) -> str:
    return theme.color_for(
        cost_source,
        cost_sources,
        explicit=theme.COST_SOURCE_COLORS,
    )


@dataclass(frozen=True)
class _CoverageSegment:
    bar_html: str
    legend: str


def _coverage_segment(
    row: CostCoverageRow,
    weight: int,
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> _CoverageSegment:
    pct = weight / total * 100
    color = _cost_source_color(row.cost_source, cost_sources, theme)
    return _CoverageSegment(
        bar_html=(
            f'<span style="width:{pct:.1f}%;background:{color}" '
            f'title="{html.escape(row.cost_source)}"></span>'
        ),
        legend=(
            f'<span><span class="dot" style="background:{color}"></span>'
            f'{html.escape(row.cost_source)} '
            f'<b style="color:{theme.TEXT};'
            f'font-family:{theme.MONO_FONT_FAMILY}">{pct:.1f}%</b>'
            '</span>'
        ),
    )


def _coverage_segments(
    rows: Sequence[CostCoverageRow],
    weights: Sequence[int],
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> list[_CoverageSegment]:
    return [
        _coverage_segment(row, weight, total, cost_sources, theme)
        for row, weight in zip(rows, weights)
    ]


def _cost_coverage_bar_html(
    rows: Sequence[CostCoverageRow], *, theme
) -> str:
    """Render the cost-attribution coverage bar to inline HTML.

    Segments are sized by token share, not run share -- a few
    high-token runs can dominate cost while looking like a thin slice
    of the run count, so the bar follows the standalone mock and sizes
    by `total_tokens`. Falls back to the run-count share only when the
    window carries no token volume yet (a fresh database with
    `agent_exit` rows that never reported usage). Colors / formatters
    come from the caller's `dashboard_theme` handle.
    """
    weights, total = _cost_coverage_weights(rows)
    segments = _coverage_segments(
        rows, weights, total, [row.cost_source for row in rows], theme
    )
    bars = "".join(segment.bar_html for segment in segments)
    legends = "".join(segment.legend for segment in segments)
    return (
        '<div class="orch-cov-title">'
        'Cost attribution coverage</div>'
        f'<div class="orch-cov-bar">{bars}</div>'
        f'<div class="orch-cov-legend">{legends}</div>'
    )


def _reliability_tiles_html(
    tiles: Sequence[tuple], *, fmt_num
) -> str:
    """Render the reliability-tile strip to inline HTML.

    Each tile is a `(value, label, tone)` triple from
    `dashboard_kpis.reliability_tile_data`; numeric values format
    through the caller's `fmt_num`, string values (e.g. the `0%`
    success rate) pass through verbatim. The `tone` class paints the
    warn / bad tiles so a window's failures and timeouts stand out.
    """
    tiles_html = "".join(
        f'<div class="orch-rel-tile {tone}">'
        f'<div class="orch-rel-value">'
        f'{html.escape(tile_value if isinstance(tile_value, str) else fmt_num(tile_value))}'
        f'</div>'
        f'<div class="orch-rel-label">{html.escape(lbl)}</div>'
        '</div>'
        for tile_value, lbl, tone in tiles
    )
    return f'<div class="orch-rel-tiles">{tiles_html}</div>'
