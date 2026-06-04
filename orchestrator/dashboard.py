# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit analytics dashboard.

Renders the redesigned `Orchestrator Analytics` page (#341) over the
read model populated by `orchestrator.analytics.sync`. The layout
mirrors the standalone HTML mock the issue ships:

- A top bar with the page title, the data extent / repo / event
  summary, and the in-range spend pill.
- A filter bar carrying the `3D` / `7D` / `All` preset selector and
  the two-date custom range.
- Computed insight banners (failure rate, cost trend, unpriced cost
  coverage).
- A four-tile KPI strip (total spend, total tokens, cost / resolved
  issue, rework share) with previous-window deltas.
- A grid of cards: hero spend / token usage stacked-area chart,
  per-stage cost bars, per-review-round cost bars, top-cost issues
  table, per-backend efficiency cards + cost-source coverage bar,
  per-repo cost bars, reliability tiles + resolved-per-day chart,
  weekday-by-hour activity heatmap.
- Per-issue drill-down at the bottom when an issue number is
  entered in the sidebar.

Reads go through `orchestrator.analytics.read` (which already
handles unset DB, connection errors, and lazy psycopg import) and
are wrapped in `st.cache_data` keyed by `(start, end, repo, events,
stages, issue)` so every widget sees the same window.

Streamlit (and its transitive pandas), `plotly`, the chart builders
in `orchestrator.dashboard_charts`, and the theme tokens in
`orchestrator.dashboard_theme` are imported *lazily* inside `main()`
so the polling tick's `orchestrator.*` import surface stays free of
the dashboard's dependency footprint. The module loads without
`streamlit` or `plotly` installed -- only `streamlit run
orchestrator/dashboard.py` (or a direct `main()` call) materializes
the imports. Tests for the pure helpers below do not need Streamlit
installed; the lazy-import invariant is asserted by
`tests/test_dashboard.py`.

Run:
    uv sync --group dashboard
    uv run streamlit run orchestrator/dashboard.py
"""
from __future__ import annotations

import html
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

# Streamlit's documented launch -- `streamlit run orchestrator/dashboard.py`
# -- executes this file as a top-level script via `runpy` with no parent
# package. The Streamlit launcher prepends the script's own directory
# (i.e. `orchestrator/`) to `sys.path`, NOT the repo root, so a
# `from . import ...` raises `ImportError: attempted relative import with
# no known parent package` before any Streamlit code can render and a
# bare `from orchestrator import ...` would fail too. Adding the repo
# root (parent of `orchestrator/`) to `sys.path` makes the absolute
# import below work in both contexts: script-launched and package-
# imported (`import orchestrator.dashboard`). The insert is idempotent
# -- in the package case the entry is already present and the check
# is a no-op.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator import analytics  # noqa: E402
from orchestrator.analytics import read as analytics_read  # noqa: E402
from orchestrator.analytics.read import (  # noqa: E402
    CostCoverageRow,
    DataExtent,
    IssueSummaryRow,
    Summary,
)

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 7
DEFAULT_RECENT_AGENT_EXITS = 100
DEFAULT_EXPENSIVE_LIMIT = 8

# Plotly config passed to every `st.plotly_chart` call. Disabling
# the modebar keeps the hover camera/zoom/pan toolbar off the cards
# -- the standalone mock has no chart chrome, and the toolbar pops
# on hover for every chart on the page otherwise.
PLOTLY_CONFIG: dict[str, Any] = {"displayModeBar": False}

# Sidebar window presets. `Custom` keeps the two-date picker so the
# operator can pin an arbitrary window inside the data extent. The
# redesigned topbar exposes the presets inline (`3D` / `7D` / `All`)
# matching the standalone mock; `Custom` stays available in the
# sidebar so the existing per-issue drill-down workflow still works.
PRESET_3D = "3d"
PRESET_7D = "7d"
PRESET_ALL = "All"
PRESET_CUSTOM = "Custom"
PRESET_OPTIONS: tuple[str, ...] = (PRESET_3D, PRESET_7D, PRESET_ALL, PRESET_CUSTOM)
PRESET_LABELS: dict[str, str] = {
    PRESET_3D: "Last 3 days",
    PRESET_7D: "Last 7 days",
    PRESET_ALL: "All time",
    PRESET_CUSTOM: "Custom range",
}
PRESET_INLINE_LABELS: dict[str, str] = {
    PRESET_3D: "3D",
    PRESET_7D: "7D",
    PRESET_ALL: "All",
}
PRESET_DAYS: dict[str, int] = {PRESET_3D: 3, PRESET_7D: 7}
DEFAULT_PRESET = PRESET_ALL

# Insight thresholds.
FAILURE_RATE_BANNER_THRESHOLD = 0.10
COST_DELTA_BANNER_THRESHOLD = 0.25
UNPRICED_COVERAGE_THRESHOLD = 0.10
UNPRICED_COST_SOURCES: frozenset[str] = frozenset({"unknown-price", "unknown"})
# Bucket strings the review-round breakdown emits whose runs are
# "rework" (i.e. happened after the initial pass). Used to compute the
# rework share KPI. `get_review_round_breakdown` keeps rounds 3, 4 and
# 5 separate (only 6+ is grouped), so every post-initial round is
# listed explicitly here.
REWORK_BUCKETS: frozenset[str] = frozenset(
    {"1", "2", "3", "4", "5", "6+"}
)

# Parallel read fan-out for `main()`'s 13 independent widget reads.
# Opt-in via `DASHBOARD_PARALLEL_READS` so the new path can be A/B'd
# against the sequential baseline. Default off: the reads keep running
# one-at-a-time on the Streamlit render thread unless the operator
# flips this on. Truthy spellings follow the same vocabulary as other
# boolean knobs in the codebase (`DECOMPOSE` etc.). Parsed at module
# import like `ANALYTICS_DB_URL`, so a Streamlit restart picks up the
# change without per-render env reads.
PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"
PARALLEL_READS_MAX_WORKERS = 8
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def _parse_parallel_reads_flag() -> bool:
    raw = os.environ.get(PARALLEL_READS_ENV, "").strip().lower()
    return raw in _TRUTHY


DASHBOARD_PARALLEL_READS: bool = _parse_parallel_reads_flag()

UNCONFIGURED_DB_MESSAGE = (
    "`ANALYTICS_DB_URL` is not configured. Set it in your environment "
    "(see `.env.example.advanced` and `docs/configuration.md`) and "
    "reload the dashboard to view analytics."
)
NO_DATA_MESSAGE = (
    "No analytics events have been recorded yet. Run "
    "`uv run python -m orchestrator.analytics.sync` after some "
    "workflow activity to populate the dashboard."
)
EMPTY_WINDOW_MESSAGE = (
    "No analytics events match the current filters. Broaden the window "
    "or clear a filter to see activity."
)


@dataclass(frozen=True)
class DateWindow:
    """Inclusive-start, exclusive-end datetime window."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class InsightBanner:
    """A single banner line displayed at the top of the page.

    `severity` is one of `success` / `info` / `warning` / `error`;
    the dashboard renders each through the matching coloured insight
    block. Keeping severity a plain string (rather than an Enum)
    means the helpers stay importable without Streamlit and the
    tests can compare against string literals.
    """

    severity: str
    message: str


def default_date_range(
    *,
    today: Optional[date] = None,
    days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[date, date]:
    """Default `[start, end]` inclusive date range for the sidebar.

    Kept for callers and tests that pre-dated the data-extent-bounded
    preset selector. `today` injection keeps this testable; the
    production path relies on `date.today()`. `days` is clamped at 1
    so `days=0` (an explicit "today only" choice) still returns
    `(today, today)` rather than a reversed range.
    """
    end = today or date.today()
    start = end - timedelta(days=max(days - 1, 0))
    return start, end


def to_window(start_date: date, end_date: date) -> DateWindow:
    """Convert inclusive `[start_date, end_date]` to a `DateWindow`."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return DateWindow(start=start_dt, end=end_dt)


def _extent_dates(extent: DataExtent) -> Optional[tuple[date, date]]:
    """`(min_date, max_date)` from a data extent, or `None` when empty."""
    if extent.min_ts is None or extent.max_ts is None:
        return None
    return extent.min_ts.date(), extent.max_ts.date()


def preset_window(
    preset: str, extent: DataExtent
) -> Optional[DateWindow]:
    """Resolve a sidebar preset into a data-extent-bounded `DateWindow`.

    The presets are anchored at the data extent's max date (not
    today) so a freshly-deployed Postgres whose latest event is days
    old still surfaces the last days of recorded activity. Returns
    `None` when the extent is empty (no events yet) or `preset` is
    `Custom` (the caller renders a date-range picker instead). An
    unknown preset string also returns `None`.

    For `3d` / `7d`, the start date is clamped to
    `max(extent.min_date, max_date - (n - 1))` so short data histories
    do not produce windows reaching before the first recorded event.
    """
    bounds = _extent_dates(extent)
    if bounds is None:
        return None
    min_d, max_d = bounds
    if preset == PRESET_ALL:
        return to_window(min_d, max_d)
    days = PRESET_DAYS.get(preset)
    if days is None:
        return None
    start_d = max(max_d - timedelta(days=days - 1), min_d)
    return to_window(start_d, max_d)


def previous_window(window: DateWindow) -> DateWindow:
    """Window of the same length ending at `window.start`."""
    length = window.end - window.start
    return DateWindow(start=window.start - length, end=window.start)


def kpi_delta(
    current: float, previous: float
) -> Optional[float]:
    """Relative change vs the previous window.

    Returns `(current - previous) / previous` (e.g. `0.25` = +25%) or
    `None` when `previous` is zero / negative so the dashboard hides
    the delta indicator rather than rendering an infinity. Negative
    `previous` values are not expected in this column set (counts,
    spend, tokens are all non-negative) but the guard keeps the
    helper safe to call from anywhere.
    """
    if previous <= 0:
        return None
    return (current - previous) / previous


def parse_issue_number(raw: str) -> Optional[int]:
    """Lenient `#123` / `123` parser for the drill-down input."""
    if not raw:
        return None
    s = raw.strip().lstrip("#").strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n > 0 else None


def db_unconfigured_message() -> Optional[str]:
    """Single source of truth for the "no DB configured" banner."""
    if not analytics.ANALYTICS_DB_URL:
        return UNCONFIGURED_DB_MESSAGE
    return None


def dashboard_parallel_reads_enabled() -> bool:
    """True when `DASHBOARD_PARALLEL_READS` is set to a truthy sentinel.

    Default OFF -- the parallel fan-out is opt-in so operators can A/B
    against the sequential baseline before flipping it on. Truthy
    values: `1` / `true` / `on` / `yes` (case-insensitive). Anything
    else (including empty / unset) keeps the sequential path. Reads
    the module-level `DASHBOARD_PARALLEL_READS` so tests can patch the
    attribute directly (mirrors how `analytics.ANALYTICS_DB_URL` is
    exposed).
    """
    return DASHBOARD_PARALLEL_READS


def _fan_out_reads(
    readers: Sequence[tuple[str, Callable[[], Any]]],
    *,
    parallel: bool,
    max_workers: int = PARALLEL_READS_MAX_WORKERS,
) -> dict[str, Any]:
    """Run each `(name, callable)` reader and return `{name: result}`.

    `parallel=False` runs readers one-at-a-time in submission order on
    the calling thread -- the sequential baseline. The thread-local
    `analytics_connection` keeps the single psycopg socket warm across
    all 13 reads.

    `parallel=True` dispatches across a `ThreadPoolExecutor` capped at
    `max_workers`. Each worker thread opens its own thread-local
    analytics connection on first use and reuses it across whatever
    subset of the readers lands on it -- `psycopg.Connection` is not
    thread-safe for concurrent use, so sharing one socket across
    workers would corrupt the wire protocol; the per-thread cache in
    `analytics.read.analytics_connection` keeps each worker's socket
    isolated. The wall-clock collapses to roughly the slowest reader
    in a wave of `max_workers` plus a small executor overhead.

    Any exception raised by a reader propagates verbatim from the
    first failing future (matching the sequential path's "stop at the
    first error" shape) so the caller can surface a single
    user-friendly `AnalyticsReadError` message. Results are returned
    in `readers` submission order so the call sites can unpack them
    without caring about completion order.
    """
    if not parallel:
        return {name: fn() for name, fn in readers}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in readers]
        return {name: fut.result() for name, fut in futures}


def resolve_stage_filter(
    selected: Sequence[str],
    available: Sequence[str],
) -> Optional[list[str]]:
    """Resolve the sidebar stage multiselect into a read-model filter.

    See module docstring of `orchestrator.analytics.read` for the
    tri-state contract (`None` = no filter, `[]` = FALSE, non-empty
    = `IN (...)`). The default "all selected" must collapse to `None`
    so NULL-stage rows are not silently excluded.
    """
    if not available:
        return None
    if set(selected) == set(available):
        return None
    return list(selected)


def cache_key(
    window: DateWindow,
    repo: Optional[str],
    events: Optional[Sequence[str]],
    stages: Optional[Sequence[str]],
    issue: Optional[int],
) -> tuple:
    """Hashable cache key for the dashboard's window-scoped reads."""
    events_t = tuple(events) if events is not None else None
    stages_t = tuple(stages) if stages is not None else None
    return (window.start, window.end, repo, events_t, stages_t, issue)


def compute_insights(
    summary: Summary,
    *,
    prev_summary: Optional[Summary] = None,
    cost_coverage_rows: Sequence[CostCoverageRow] = (),
) -> list[InsightBanner]:
    """Banner lines surfaced at the top of the redesigned page.

    Each banner is a single observation the operator should act on:

    - Failure rate exceeds `FAILURE_RATE_BANNER_THRESHOLD`: agent
      runs are exiting non-zero more than 10 % of the time.
    - Cost trend exceeds `COST_DELTA_BANNER_THRESHOLD` versus the
      previous window: a sustained 25 % swing is worth surfacing
      even though the KPI row already shows the delta.
    - Unpriced cost coverage exceeds `UNPRICED_COVERAGE_THRESHOLD`:
      the pricing table in `orchestrator.usage` is missing SKUs the
      parser is seeing in the wild.

    The helper returns an empty list when nothing crosses a
    threshold, so the caller can branch on `if banners:` for the
    section header.
    """
    banners: list[InsightBanner] = []
    if summary.total_agent_runs > 0:
        rate = summary.failed_agent_runs / summary.total_agent_runs
        if rate >= FAILURE_RATE_BANNER_THRESHOLD:
            banners.append(
                InsightBanner(
                    severity="error",
                    message=(
                        f"{summary.failed_agent_runs} of "
                        f"{summary.total_agent_runs} agent runs failed "
                        f"({rate * 100:.0f}%)."
                    ),
                )
            )
    if prev_summary is not None:
        delta = kpi_delta(
            summary.total_cost_usd, prev_summary.total_cost_usd
        )
        if delta is not None and abs(delta) >= COST_DELTA_BANNER_THRESHOLD:
            direction = "up" if delta > 0 else "down"
            severity = "warning" if delta > 0 else "info"
            banners.append(
                InsightBanner(
                    severity=severity,
                    message=(
                        f"Total cost is {direction} "
                        f"{abs(delta) * 100:.0f}% vs the previous window "
                        f"(${summary.total_cost_usd:,.2f} vs "
                        f"${prev_summary.total_cost_usd:,.2f})."
                    ),
                )
            )
    if cost_coverage_rows:
        total_runs = sum(r.runs for r in cost_coverage_rows)
        unpriced = sum(
            r.runs
            for r in cost_coverage_rows
            if r.cost_source in UNPRICED_COST_SOURCES
        )
        if total_runs > 0:
            ratio = unpriced / total_runs
            if ratio >= UNPRICED_COVERAGE_THRESHOLD:
                banners.append(
                    InsightBanner(
                        severity="warning",
                        message=(
                            f"{unpriced} of {total_runs} agent runs lack "
                            f"a priced cost ({ratio * 100:.0f}%) -- check "
                            "the pricing table in `orchestrator.usage` "
                            "for missing SKUs."
                        ),
                    )
                )
    return banners


def reliability_tile_data(
    summary: Summary,
    *,
    resolved: int = 0,
    rejected: int = 0,
) -> list[tuple[int, str, str]]:
    """`(value, label, tone)` triples for the six reliability tiles.

    Extracted from `main()` so the wiring stays testable without a
    live Streamlit run: every tile sources its number from a
    full-window aggregate on `Summary` (`total_agent_runs`,
    `failed_agent_runs`, `timed_out_agent_runs`) so a long window
    with more than `DEFAULT_RECENT_AGENT_EXITS` rows never silently
    undercounts the tile -- earlier drafts read timeouts off
    `get_recent_agent_exits` and missed any timeout outside the
    latest 100 rows.

    `resolved` / `rejected` are the per-day rollups summed by the
    caller from `get_throughput_breakdown`; they default to zero so
    callers that only care about the agent-run tiles can ignore the
    throughput axis.

    Tones (`"good"` / `"warn"` / `"bad"` / `""`) drive the CSS class
    applied to the tile; the caller never has to recompute them.
    """
    total_runs = int(summary.total_agent_runs or 0)
    failed = int(summary.failed_agent_runs or 0)
    timed_out = int(summary.timed_out_agent_runs or 0)
    success_pct = (
        (1.0 - failed / total_runs) * 100
        if total_runs > 0 else 0.0
    )
    return [
        (total_runs, "Agent runs", ""),
        (f"{success_pct:.0f}%", "Success rate", "good"),
        (int(resolved), "Resolved", "good"),
        (int(rejected), "Rejected", "warn" if rejected else ""),
        (failed, "Failures", "warn" if failed else ""),
        (timed_out, "Timeouts", "bad" if timed_out else ""),
    ]


def top_expensive_issues(
    rows: Sequence[IssueSummaryRow],
    *,
    limit: int = DEFAULT_EXPENSIVE_LIMIT,
) -> list[IssueSummaryRow]:
    """Issues sorted by total cost desc for the "where did spend go" table."""
    if limit <= 0:
        return []

    def _key(r: IssueSummaryRow) -> tuple:
        cost = r.total_cost_usd if r.total_cost_usd is not None else -1.0
        return (-cost, -int(r.event_count), r.repo, int(r.issue))

    return sorted(rows, key=_key)[:limit]


def rework_totals(
    rows: Sequence[Any],
) -> tuple[float, float]:
    """Return `(total_cost, rework_cost)` across review-round buckets.

    `rework_cost` sums the cost of every row whose `bucket` is in
    `REWORK_BUCKETS` (i.e. review round >= 1). `total_cost` sums
    every row, including the initial pass. Cost defaults to `0.0`
    when the row predates the `total_cost_usd` column.
    """
    total = sum(
        float(getattr(r, "total_cost_usd", 0.0) or 0.0) for r in rows
    )
    rework = sum(
        float(getattr(r, "total_cost_usd", 0.0) or 0.0)
        for r in rows
        if r.bucket in REWORK_BUCKETS
    )
    return total, rework


def _sparkline_svg(
    values: Sequence[float], *, color: str, w: int = 96, h: int = 26
) -> str:
    """Inline SVG sparkline for KPI cards.

    Renders a filled curve under the polyline; rendering is HTML-only
    so the dashboard can drop it inside `st.markdown(..., unsafe_allow_html=True)`
    without a chart round-trip. Empty / flat data renders an empty SVG
    so the layout slot stays consistent across KPIs.
    """
    nums = [float(v or 0) for v in values]
    if not nums or max(nums) == min(nums) == 0:
        return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"></svg>'
    lo, hi = min(nums), max(nums)
    span = max(hi - lo, 1e-9)
    pad = 2
    step = (w - pad * 2) / max(len(nums) - 1, 1)

    def y(v: float) -> float:
        return pad + (1 - (v - lo) / span) * (h - pad * 2)

    points = [(pad + i * step, y(v)) for i, v in enumerate(nums)]
    poly = " ".join(f"{x:.1f},{yv:.1f}" for x, yv in points)
    area_path = (
        "M" + f"{points[0][0]:.1f},{h - pad:.1f}"
        + " L" + " L".join(f"{x:.1f},{yv:.1f}" for x, yv in points)
        + f" L{points[-1][0]:.1f},{h - pad:.1f} Z"
    )
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'style="display:block">'
        f'<path d="{area_path}" fill="{color}" fill-opacity="0.18" />'
        f'<polyline points="{poly}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round" />'
        "</svg>"
    )


def _delta_pill(value: Optional[float], *, invert: bool = False) -> str:
    """Render a KPI delta pill (▲/▼ NN.N%) as inline HTML.

    Color convention -- ``.orch-delta.up`` is red, ``.orch-delta.down``
    is green. With ``invert=False`` (the default) a rising value paints
    red and a falling value paints green: this is the right convention
    for cost / token KPIs where "up is bad". ``invert=True`` swaps the
    coloring so positive growth paints green -- use it for KPIs where
    "up is good" (e.g. issues resolved, success rate). The arrow always
    follows the value's sign so the direction is unambiguous even at a
    glance.

    ``None`` renders a flat dash so the layout slot stays consistent.
    """
    if value is None:
        return '<span class="orch-delta flat">—</span>'
    pct_str = f"{abs(value) * 100:.1f}%"
    if value > 0:
        cls = "up" if not invert else "down"
        arrow = "▲"
    elif value < 0:
        cls = "down" if not invert else "up"
        arrow = "▼"
    else:
        return f'<span class="orch-delta flat">— {pct_str}</span>'
    return f'<span class="orch-delta {cls}">{arrow} {pct_str}</span>'


def _topbar_html(
    *,
    extent: DataExtent,
    distinct_repos: int,
    total_events: int,
    spend_in_range: float,
    fmt_money_exact,
    fmt_num,
) -> str:
    """Render the page topbar block.

    Mirrors the standalone mock's brand mark + h1 + spend pill.
    """
    if extent.min_ts is None or extent.max_ts is None:
        range_label = "no data recorded yet"
    else:
        range_label = (
            f"{extent.min_ts.date().isoformat()} → "
            f"{extent.max_ts.date().isoformat()} available"
        )
    sub = (
        f"{html.escape(range_label)} · "
        f"{distinct_repos} repo{'s' if distinct_repos != 1 else ''} · "
        f"{fmt_num(total_events)} events"
    )
    return (
        '<div class="orch-topbar">'
        '<div class="orch-brand">'
        '<span class="orch-brand-mark">OA</span>'
        '<div>'
        '<h1>Orchestrator Analytics</h1>'
        f'<p class="orch-sub">{sub}</p>'
        '</div></div>'
        '<div class="orch-spend">'
        '<span class="label">Spend in range</span>'
        f'<span class="value">{html.escape(fmt_money_exact(spend_in_range))}</span>'
        '</div></div>'
    )


def _filter_meta_html(
    *,
    from_d: date, to_d: date, days: int, runs: int, fmt_num
) -> str:
    return (
        '<div class="orch-filter-meta">'
        f'{from_d.isoformat()} → {to_d.isoformat()} · '
        f'{days} day{"s" if days != 1 else ""} · '
        f'{fmt_num(runs)} runs'
        '</div>'
    )


def _kpi_strip_html(kpis: Sequence[dict]) -> str:
    """Render the four-tile KPI strip.

    Each KPI dict carries `label`, `value`, `delta`, `sub`,
    optionally `spark` (list of floats) and `spark_color`.
    """
    cells = []
    for k in kpis:
        delta_html = _delta_pill(
            k.get("delta"), invert=k.get("invert", False)
        )
        spark_html = ""
        if k.get("spark") is not None:
            spark_html = _sparkline_svg(
                k["spark"], color=k.get("spark_color", "#5b54e0")
            )
        cells.append(
            '<div class="orch-kpi">'
            '<div class="kpi-top">'
            f'<span class="kpi-label">{html.escape(k["label"])}</span>'
            f'{delta_html}'
            '</div>'
            f'<div class="kpi-value">{html.escape(str(k["value"]))}</div>'
            '<div class="kpi-foot">'
            f'<span>{html.escape(str(k.get("sub", "")))}</span>'
            f'{spark_html}'
            '</div></div>'
        )
    return '<div class="orch-kpis">' + "".join(cells) + '</div>'


def _issues_table_html(rows: Sequence[IssueSummaryRow]) -> str:
    """Render the "Most expensive issues" table to inline HTML.

    Matches the standalone mock's columns -- Issue / Cost / Runs /
    Review rds / Retries / Status -- and adds two representational
    details `st.dataframe` cannot express:

    - **In-row cost bars.** Each Issue cell carries a thin bar
      under the label whose width is the issue's cost relative to
      the most expensive issue in the panel. Lets the operator
      eyeball the spread without comparing numbers row by row.
    - **Clean / fail status pills.** The Status cell renders as a
      colored pill (`clean` is green, `N fail` is red) instead of
      flat text, matching the mock's pill treatment.

    Local CSS goes inline next to the table so the rules survive a
    future tweak without having to touch `dashboard_theme.PAGE_CSS`
    -- the issues table is the only consumer.
    """
    max_cost = max(
        (float(r.total_cost_usd or 0.0) for r in rows),
        default=0.0,
    ) or 1.0
    css = """
<style>
  .orch-issues { width: 100%; border-collapse: collapse;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 12.5px; }
  .orch-issues thead th { color: var(--orch-muted);
    font-size: 11px; font-weight: 500; letter-spacing: 0.05em;
    text-transform: uppercase; text-align: left;
    padding: 4px 6px 8px; border-bottom: 1px solid var(--orch-border); }
  .orch-issues thead th.r { text-align: right; }
  .orch-issues tbody td { padding: 8px 6px; vertical-align: middle;
    border-bottom: 1px solid var(--orch-grid); }
  .orch-issues tbody tr:last-child td { border-bottom: 0; }
  .orch-issues td.r { text-align: right; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; color: var(--orch-ink); }
  .orch-issues td.strong { font-weight: 600; }
  .orch-issue-cell { display: flex; flex-direction: column;
    gap: 4px; }
  .orch-issue-name { color: var(--orch-ink); font-weight: 500; }
  .orch-issue-num { color: var(--orch-muted); font-weight: 400;
    margin-left: 2px; }
  .orch-issue-bar { display: block; height: 4px; border-radius: 2px;
    background: var(--orch-grid); overflow: hidden; }
  .orch-issue-bar > span { display: block; height: 100%;
    background: var(--orch-accent); border-radius: 2px; }
  .orch-pill { display: inline-block; padding: 2px 9px;
    border-radius: 999px; font-size: 11.5px; font-weight: 500;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
  .orch-pill.ok { background: rgba(26, 163, 154, 0.14);
    color: var(--orch-success); }
  .orch-pill.bad { background: rgba(217, 83, 74, 0.14);
    color: var(--orch-danger); }
  .orch-badge-warn { color: var(--orch-warn); font-weight: 600; }
</style>
"""
    body: list[str] = []
    for r in rows:
        short = r.repo.split("/")[-1] if "/" in r.repo else r.repo
        cost = float(r.total_cost_usd or 0.0)
        bar_pct = (cost / max_cost * 100.0) if max_cost > 0 else 0.0
        cost_text = (
            f"${r.total_cost_usd:,.2f}"
            if r.total_cost_usd is not None
            else "—"
        )
        review_rounds = (
            int(r.max_review_round)
            if r.max_review_round is not None
            else 0
        )
        retries = (
            int(r.max_retry_count)
            if r.max_retry_count is not None
            else 0
        )
        failed = int(r.failed_agent_runs or 0)
        if failed:
            pill = f'<span class="orch-pill bad">{failed} fail</span>'
        else:
            pill = '<span class="orch-pill ok">clean</span>'
        # High review-round counts get a warning color so the
        # operator can spot rework-heavy issues without reading the
        # number.
        review_html = (
            f'<span class="orch-badge-warn">{review_rounds}</span>'
            if review_rounds >= 3
            else str(review_rounds)
        )
        body.append(
            "<tr>"
            "<td>"
            '<div class="orch-issue-cell">'
            f'<span><span class="orch-issue-name">{html.escape(short)}</span>'
            f' <span class="orch-issue-num">#{int(r.issue)}</span></span>'
            f'<span class="orch-issue-bar"><span style="width:{bar_pct:.1f}%">'
            "</span></span>"
            "</div>"
            "</td>"
            f'<td class="r strong">{html.escape(cost_text)}</td>'
            f'<td class="r">{int(r.agent_exits or 0)}</td>'
            f'<td class="r">{review_html}</td>'
            f'<td class="r">{retries}</td>'
            f'<td class="r">{pill}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr>"
        "<th>Issue</th>"
        '<th class="r">Cost</th>'
        '<th class="r">Runs</th>'
        '<th class="r">Review rds</th>'
        '<th class="r">Retries</th>'
        '<th class="r">Status</th>'
        "</tr></thead>"
    )
    return (
        css
        + '<table class="orch-issues">'
        + head
        + "<tbody>" + "".join(body) + "</tbody>"
        + "</table>"
    )


def _card_header_html(title: str, subtitle: str = "") -> str:
    """Inline HTML for the title + subtitle at the top of a card.

    Always rendered through `st.markdown(unsafe_allow_html=True)`
    INSIDE a `st.container(border=True)` block -- a previous draft
    opened a `<div class="orch-card">` in one `st.markdown` and
    closed it in another, which leaves the chart / dataframe widget
    as a sibling of the card in Streamlit's DOM rather than a child.
    The card visual really has to come from a Streamlit container so
    the inner widgets sit inside it.
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
    for b in banners:
        icon = icon_for.get(b.severity, "›")
        rows.append(
            f'<div class="orch-insight {html.escape(b.severity)}">'
            f'<span class="icon">{icon}</span>'
            f'<span>{html.escape(b.message)}</span>'
            '</div>'
        )
    return '<div class="orch-insights">' + "".join(rows) + '</div>'


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit, pandas, plotly, the chart builders, and the
    theme tokens lazily so the orchestrator polling path never pulls
    them in. Run via `streamlit run orchestrator/dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import pandas as pd
    import streamlit as st

    from orchestrator import dashboard_charts, dashboard_theme as theme

    st.set_page_config(
        page_title="Orchestrator Analytics",
        layout="wide",
    )
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)

    unset = db_unconfigured_message()
    if unset:
        st.warning(unset)
        st.stop()

    try:
        with analytics_read.analytics_connection() as conn:
            extent = analytics_read.get_data_extent(conn=conn)
            options = analytics_read.get_filter_options(conn=conn)
    except analytics_read.AnalyticsReadError as e:
        st.error(
            "Could not load analytics filter options: "
            f"{e}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()

    if extent.min_ts is None or extent.max_ts is None:
        st.markdown(
            _topbar_html(
                extent=extent,
                distinct_repos=0,
                total_events=0,
                spend_in_range=0.0,
                fmt_money_exact=theme.fmt_money_exact,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )
        st.info(NO_DATA_MESSAGE)
        st.stop()

    extent_min_d = extent.min_ts.date()
    extent_max_d = extent.max_ts.date()

    with st.sidebar:
        st.header("Filters")
        repo_options = ("All", *options.repos) if options.repos else ("All",)
        repo_choice = st.selectbox("Repo", repo_options, index=0)
        event_choice = st.multiselect(
            "Events",
            list(options.events),
            default=list(options.events),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these events'."
            ),
        )
        stage_choice = st.multiselect(
            "Stages",
            list(options.stages),
            default=list(options.stages),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these stages'."
            ),
        )
        issue_input = st.text_input(
            "Issue number",
            value="",
            help=(
                "Enter `123` or `#123` to narrow every widget to one "
                "issue AND render the per-issue event trace at the "
                "bottom. Requires a specific repo above."
            ),
        )

    # Topbar: title + spend pill. We render a placeholder spend now
    # so it occupies the right slot; we replace it after the summary
    # query lands below.
    topbar_slot = st.empty()

    # Filter bar: presets + date inputs + range meta inside a bordered
    # container styled as the "Date range" card. Preset state persists
    # in session_state so a custom date pick survives reruns.
    if "preset" not in st.session_state:
        st.session_state.preset = DEFAULT_PRESET
    with st.container(border=True):
        # A hidden `.orch-cardmark` as the bordered container's first
        # child lets the shared white-card rule in
        # `dashboard_theme.PAGE_CSS` (`:has(> stElementContainer
        # .orch-cardmark)`) paint this filter bar like every other card --
        # Streamlit 1.58 dropped the stable border-wrapper testid the old
        # per-card selector relied on. The `.orch-filterbar-anchor` below
        # stays in the left column purely as the hidden label sentinel.
        st.markdown(
            '<div class="orch-cardmark"></div>', unsafe_allow_html=True
        )
        fb_left, fb_mid, fb_right = st.columns([2, 3, 3])
        with fb_left:
            st.markdown(
                '<div class="orch-filterbar-anchor"></div>'
                '<span class="orch-filter-label">Date range</span>',
                unsafe_allow_html=True,
            )
            preset_choice = st.radio(
                "Range preset",
                options=(PRESET_3D, PRESET_7D, PRESET_ALL),
                format_func=lambda p: PRESET_INLINE_LABELS[p],
                index=(
                    (PRESET_3D, PRESET_7D, PRESET_ALL).index(
                        st.session_state.preset
                    )
                    if st.session_state.preset
                    in (PRESET_3D, PRESET_7D, PRESET_ALL)
                    else 2
                ),
                horizontal=True,
                label_visibility="collapsed",
                key="_preset_radio",
            )
        initial_window = (
            preset_window(preset_choice, extent)
            or to_window(extent_min_d, extent_max_d)
        )
        with fb_mid:
            c1, c2 = st.columns(2)
            with c1:
                start_date = st.date_input(
                    "From",
                    value=initial_window.start.date(),
                    min_value=extent_min_d,
                    max_value=extent_max_d,
                )
            with c2:
                end_default = (initial_window.end - timedelta(days=1)).date()
                end_date = st.date_input(
                    "To",
                    value=end_default,
                    min_value=extent_min_d,
                    max_value=extent_max_d,
                )
    window = to_window(start_date, end_date)
    st.session_state.preset = preset_choice

    repo_filter = None if repo_choice == "All" else repo_choice
    issue_input_parsed = parse_issue_number(issue_input)
    issue_filter = (
        issue_input_parsed if repo_filter is not None else None
    )
    event_filter = list(event_choice)
    stage_filter = resolve_stage_filter(stage_choice, options.stages)

    key = cache_key(
        window, repo_filter, event_filter, stage_filter, issue_filter
    )
    prev_w = previous_window(window)
    prev_key = cache_key(
        prev_w, repo_filter, event_filter, stage_filter, issue_filter
    )

    # Connection scoping: each cached wrapper checks out a thread-local
    # analytics connection inside its body via `analytics_connection()`
    # rather than threading a connection through the cache key (a raw
    # psycopg.Connection is not hashable and would crash the wrapper,
    # and every reload would otherwise look like a cache miss). The
    # thread-local persists across wrappers in the same render pass,
    # so the first cache-miss pays the ~1 s psycopg handshake and the
    # remaining 13 reads reuse the open socket. On a broken-connection
    # error inside a `with` block the CM closes the cached socket so
    # the next wrapper opens a fresh one. The cache key stays
    # `(start, end, repo, events_t, stages_t, issue)` -- exactly the
    # filter tuple, which is what we want anyway.

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_summary(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_summary(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_time_series(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_time_series(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_stage_breakdown(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_stage_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_recent_agent_exits(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_recent_agent_exits(
                limit=DEFAULT_RECENT_AGENT_EXITS,
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_top_cost_issues(start, end, repo, events_t, stages_t, issue):
        # Ask the database for the top-cost issues directly. Reading
        # the latest N issues by `last_seen` and re-sorting in Python
        # silently drops older high-cost issues that fall outside the
        # truncated set, so the redesigned "Most expensive issues"
        # panel must be cost-ordered at the SQL layer.
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_issues(
                limit=DEFAULT_EXPENSIVE_LIMIT,
                sort_by=analytics_read.SORT_BY_COST,
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_review_round(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_review_round_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_efficiency(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_backend_efficiency(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_repo_breakdown(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_repo_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_cost_coverage(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_cost_coverage(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_hourly_heatmap(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_hourly_heatmap(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_throughput(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_throughput_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_daily_tokens(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_backend_daily_tokens(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    # Read fan-out. Each entry is `(name, zero-arg callable)` so
    # `_fan_out_reads` can dispatch them across worker threads when
    # `DASHBOARD_PARALLEL_READS` is set; the sequential path stays in
    # this thread under the existing thread-local `analytics_connection`.
    # Lambdas close over `key` / `prev_key` so the executor never has
    # to thread filter tuples through the futures.
    readers: list[tuple[str, Callable[[], Any]]] = [
        ("summary", lambda: _read_summary(*key)),
        ("prev_summary", lambda: _read_summary(*prev_key)),
        ("ts_points", lambda: _read_time_series(*key)),
        ("stage_rows", lambda: _read_stage_breakdown(*key)),
        ("agent_exits", lambda: _read_recent_agent_exits(*key)),
        ("issues_rows", lambda: _read_top_cost_issues(*key)),
        ("review_round_rows", lambda: _read_review_round(*key)),
        ("backend_rows", lambda: _read_backend_efficiency(*key)),
        ("repo_rows", lambda: _read_repo_breakdown(*key)),
        ("cost_coverage_rows", lambda: _read_cost_coverage(*key)),
        ("heatmap_rows", lambda: _read_hourly_heatmap(*key)),
        ("throughput_rows", lambda: _read_throughput(*key)),
        ("backend_daily_rows", lambda: _read_backend_daily_tokens(*key)),
    ]
    parallel = dashboard_parallel_reads_enabled()
    load_start = perf_counter()
    try:
        results = _fan_out_reads(readers, parallel=parallel)
    except analytics_read.AnalyticsReadError as e:
        st.error(
            f"Analytics query failed: {e}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()
    log.info(
        "dashboard.load: total=%.1fs reads=%d parallel=%s",
        perf_counter() - load_start,
        len(readers),
        "true" if parallel else "false",
    )
    summary = results["summary"]
    prev_summary = results["prev_summary"]
    ts_points = results["ts_points"]
    stage_rows = results["stage_rows"]
    agent_exits = results["agent_exits"]
    issues_rows = results["issues_rows"]
    review_round_rows = results["review_round_rows"]
    backend_rows = results["backend_rows"]
    repo_rows = results["repo_rows"]
    cost_coverage_rows = results["cost_coverage_rows"]
    heatmap_rows = results["heatmap_rows"]
    throughput_rows = results["throughput_rows"]
    backend_daily_rows = results["backend_daily_rows"]

    # Now we can render the topbar with the real spend value.
    topbar_slot.markdown(
        _topbar_html(
            extent=extent,
            distinct_repos=summary.distinct_repos,
            total_events=summary.total_events,
            spend_in_range=summary.total_cost_usd,
            fmt_money_exact=theme.fmt_money_exact,
            fmt_num=theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )

    days_in_window = max(
        (window.end - window.start).days, 1
    )
    with fb_right:
        st.markdown(
            _filter_meta_html(
                from_d=window.start.date(),
                to_d=(window.end - timedelta(days=1)).date(),
                days=days_in_window,
                runs=summary.total_agent_runs,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )

    if summary.total_events == 0:
        st.info(EMPTY_WINDOW_MESSAGE)
        _render_drilldown(
            st=st,
            pd=pd,
            window=window,
            repo_filter=repo_filter,
            issue_input_parsed=issue_input_parsed,
            event_filter=event_filter,
            stage_filter=stage_filter,
        )
        return

    banners = compute_insights(
        summary,
        prev_summary=prev_summary,
        cost_coverage_rows=cost_coverage_rows,
    )
    if banners:
        st.markdown(_insights_html(banners), unsafe_allow_html=True)

    # KPI strip --------------------------------------------------
    # Token totals include cache_read + cache_write so the headline
    # figure matches the standalone mock's
    # `input + output + cache_read + cache_write` accounting; the
    # `cached_tokens` cumulative column is deliberately excluded so
    # the cache band is not double-counted.
    total_cost = float(summary.total_cost_usd or 0.0)
    total_tokens = int(
        (summary.total_input_tokens or 0)
        + (summary.total_output_tokens or 0)
        + (summary.total_cache_read_tokens or 0)
        + (summary.total_cache_write_tokens or 0)
    )
    total_cost_prev = float(prev_summary.total_cost_usd or 0.0)
    total_tokens_prev = int(
        (prev_summary.total_input_tokens or 0)
        + (prev_summary.total_output_tokens or 0)
        + (prev_summary.total_cache_read_tokens or 0)
        + (prev_summary.total_cache_write_tokens or 0)
    )
    resolved = sum(int(r.resolved or 0) for r in throughput_rows)
    rejected = sum(int(r.rejected or 0) for r in throughput_rows)
    rr_total_cost, rr_rework_cost = rework_totals(review_round_rows)
    rework_share = (
        (rr_rework_cost / rr_total_cost) if rr_total_cost > 0 else 0.0
    )

    # Sparkline series, one entry per day in the window. Daily
    # tokens mirror the KPI accounting and include the cache band.
    days = sorted({p.day for p in ts_points})
    days_index = {d: i for i, d in enumerate(days)}
    daily_cost = [0.0] * len(days)
    daily_tokens = [0.0] * len(days)
    for p in ts_points:
        i = days_index[p.day]
        daily_cost[i] += float(p.cost_usd or 0.0)
        daily_tokens[i] += float(
            (p.input_tokens or 0)
            + (p.output_tokens or 0)
            + (p.cache_read_tokens or 0)
            + (p.cache_write_tokens or 0)
        )
    done_index = {r.day: int(r.resolved or 0) for r in throughput_rows}
    daily_done = [done_index.get(d, 0) for d in days]

    kpis = [
        {
            "label": "Total spend",
            "value": theme.fmt_money_exact(total_cost),
            "delta": kpi_delta(total_cost, total_cost_prev),
            "sub": (
                f"{theme.fmt_money(total_cost / days_in_window)}/day"
            ),
            "spark": daily_cost,
            "spark_color": theme.ACCENT,
        },
        {
            "label": "Total tokens",
            "value": theme.fmt_tokens(total_tokens),
            "delta": kpi_delta(total_tokens, total_tokens_prev),
            "sub": f"{theme.fmt_tokens(total_tokens / days_in_window)}/day",
            "spark": daily_tokens,
            "spark_color": theme.TOKEN_TYPE_COLORS["Input"],
        },
        {
            "label": "Cost / resolved issue",
            "value": (
                f"${total_cost / resolved:,.2f}"
                if resolved > 0 else "—"
            ),
            "delta": None,
            "sub": f"{resolved} resolved · {rejected} rejected",
            "spark": daily_done,
            "spark_color": theme.TOKEN_TYPE_COLORS["Cache"],
        },
        {
            "label": "Rework share",
            "value": f"{rework_share * 100:.0f}%",
            "delta": None,
            "sub": (
                f"{theme.fmt_money_exact(rr_rework_cost)} in review "
                "rounds >= 1"
            ),
            "spark": None,
        },
    ]
    st.markdown(_kpi_strip_html(kpis), unsafe_allow_html=True)

    # ── Hero: Spend & token usage over time ──────────────────────
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Spend & token usage over time",
                "Daily token consumption with cost trend overlaid",
            ),
            unsafe_allow_html=True,
        )
        if "stack_mode" not in st.session_state:
            st.session_state.stack_mode = "type"
        stack_mode = st.radio(
            "Stack mode",
            options=("type", "backend"),
            format_func=lambda m: (
                "By token type" if m == "type" else "By backend"
            ),
            index=0 if st.session_state.stack_mode == "type" else 1,
            horizontal=True,
            label_visibility="collapsed",
            key="_stack_mode_radio",
        )
        st.session_state.stack_mode = stack_mode

        # Build the per-day per-backend token map off
        # `get_backend_daily_tokens`, not the LIMIT-capped recent-runs
        # table -- in busy windows the cap would silently undercount
        # the "By backend" stack while the cost line and KPI tiles
        # still report the full window.
        backend_by_day: dict[date, dict[str, float]] = {}
        if stack_mode == "backend":
            for row in backend_daily_rows:
                backend_by_day.setdefault(row.day, {})
                backend_by_day[row.day][row.backend] = (
                    backend_by_day[row.day].get(row.backend, 0)
                    + int(row.total_tokens or 0)
                )

        st.plotly_chart(
            dashboard_charts.usage_over_time(
                ts_points,
                backend_rows_by_day=(
                    backend_by_day if stack_mode == "backend" else None
                ),
                mode=stack_mode,
                # The card header already renders the title; suppress
                # the in-chart title so it is not duplicated.
                title=None,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )

    # ── Stage cost (7/12) + review-round cost (5/12) ─────────────
    # Pin both bar panels to the same height (driven by whichever has
    # more bars) so the two cards line up bottom-to-bottom.
    bars_h = 40 * max(len(stage_rows), len(review_round_rows), 1) + 80
    col_stage, col_round = st.columns([7, 5])
    with col_stage:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by workflow stage",
                    "Where spend lands across the issue lifecycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_stage(stage_rows, height=bars_h),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_round:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by review round",
                    "Rework is every round after the first",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_review_round(
                    review_round_rows, height=bars_h
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )

    # ── Top expensive issues (7/12) + backend efficiency (5/12) ──
    col_issues, col_backend = st.columns([7, 5])
    with col_issues:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Most expensive issues",
                    "Cost, run count, review rounds, and failure count",
                ),
                unsafe_allow_html=True,
            )
            # `issues_rows` is already cost-ordered from the SQL
            # (`sort_by="cost"`); pipe through `top_expensive_issues`
            # so the in-memory cost / event-count tie-breakers stay
            # the source of truth and the rendered set never exceeds
            # `DEFAULT_EXPENSIVE_LIMIT`.
            expensive = top_expensive_issues(issues_rows)
            if expensive:
                st.markdown(
                    _issues_table_html(expensive),
                    unsafe_allow_html=True,
                )
            else:
                st.info("No agent runs with recorded cost in this window.")

    with col_backend:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Backend efficiency",
                    "Cost density, cache leverage, $/run",
                ),
                unsafe_allow_html=True,
            )
            if backend_rows:
                for row in backend_rows:
                    # Per-backend token total mirrors the headline KPI:
                    # input + output + cache_read + cache_write so the
                    # "cost / 1M tok" tile divides by the same volume
                    # the rest of the redesigned page reports.
                    tokens = int(
                        (row.total_input_tokens or 0)
                        + (row.total_output_tokens or 0)
                        + (row.total_cache_read_tokens or 0)
                        + (row.total_cache_write_tokens or 0)
                    )
                    cost_per_m = (
                        (row.total_cost_usd / (tokens / 1_000_000))
                        if tokens > 0 else 0.0
                    )
                    cost_per_run = (
                        (row.total_cost_usd / row.runs)
                        if row.runs > 0 else 0.0
                    )
                    # Cache leverage: share of "billable input" served
                    # from cache. Matches the standalone mock's
                    # `cacheRead / (cacheRead + input)` accounting --
                    # high cache hit means a smaller fraction of input
                    # tokens hit the model's input rate, which is the
                    # cost lever the operator is reading off the card.
                    cache_read = int(row.total_cache_read_tokens or 0)
                    input_tok = int(row.total_input_tokens or 0)
                    cache_hit_pct = (
                        (cache_read / (cache_read + input_tok) * 100)
                        if (cache_read + input_tok) > 0 else 0.0
                    )
                    color = theme.color_for(
                        row.backend, explicit=theme.BACKEND_COLORS
                    )
                    st.markdown(
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
                        f'{row.runs} runs · {theme.fmt_tokens(tokens)} tok'
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
                        f'<span>${cost_per_m:.2f} / 1M tok</span>'
                        f'<span>{cache_hit_pct:.0f}% cache hit</span>'
                        f'<span>${cost_per_run:.2f} / run</span>'
                        '</div></div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No `agent_exit` rows match the current filters.")
            if cost_coverage_rows:
                # Token share, not run share -- a few high-token runs
                # can dominate cost while looking like a thin slice of
                # the run count, so the standalone mock sizes the bar
                # by `tok` and we follow suit. Falls back to the run
                # share when the window carries no token volume yet
                # (a brand-new database with `agent_exit` rows that
                # never reported usage).
                total_tokens = sum(
                    int(r.total_tokens or 0) for r in cost_coverage_rows
                )
                if total_tokens > 0:
                    weights = [
                        int(r.total_tokens or 0) for r in cost_coverage_rows
                    ]
                    total = total_tokens
                else:
                    weights = [int(r.runs or 0) for r in cost_coverage_rows]
                    total = sum(weights) or 1
                segs = []
                legend = []
                for r, w in zip(cost_coverage_rows, weights):
                    pct = w / total * 100
                    color = theme.color_for(
                        r.cost_source,
                        [r.cost_source for r in cost_coverage_rows],
                        explicit=theme.COST_SOURCE_COLORS,
                    )
                    segs.append(
                        f'<span style="width:{pct:.1f}%;background:{color}" '
                        f'title="{html.escape(r.cost_source)}"></span>'
                    )
                    legend.append(
                        f'<span><span class="dot" '
                        f'style="background:{color}"></span>'
                        f'{html.escape(r.cost_source)} '
                        f'<b style="color:{theme.TEXT};'
                        f'font-family:{theme.MONO_FONT_FAMILY}">'
                        f'{pct:.1f}%</b>'
                        '</span>'
                    )
                st.markdown(
                    '<div class="orch-cov-title">'
                    'Cost attribution coverage</div>'
                    f'<div class="orch-cov-bar">{"".join(segs)}</div>'
                    f'<div class="orch-cov-legend">{"".join(legend)}</div>',
                    unsafe_allow_html=True,
                )

    # ── Repo cost (7/12) + reliability tiles (5/12) ─────────────
    col_repo, col_rel = st.columns([7, 5])
    with col_repo:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by repository",
                    "Spend across managed repos",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_repo(repo_rows),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_rel:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Reliability & throughput",
                    "Run health and issues resolved per day",
                ),
                unsafe_allow_html=True,
            )
            # Pull every reliability tile off the same full-window
            # `Summary` aggregate so e.g. the timeout count sees every
            # timed-out run in the window, not just the latest 100
            # rows surfaced by `get_recent_agent_exits`.
            raw_tiles = reliability_tile_data(
                summary, resolved=resolved, rejected=rejected,
            )
            tiles_html = "".join(
                f'<div class="orch-rel-tile {tone}">'
                f'<div class="orch-rel-value">'
                f'{html.escape(v if isinstance(v, str) else theme.fmt_num(v))}'
                f'</div>'
                f'<div class="orch-rel-label">{html.escape(lbl)}</div>'
                '</div>'
                for v, lbl, tone in raw_tiles
            )
            st.markdown(
                f'<div class="orch-rel-tiles">{tiles_html}</div>',
                unsafe_allow_html=True,
            )
            # Pass the window so every day -- including zero-
            # resolution ones the SQL elides -- renders an
            # explicit bar. Without the window the chart would
            # appear "gappy" against a calendar baseline.
            st.plotly_chart(
                dashboard_charts.done_per_day_bars(
                    throughput_rows,
                    window_start=window.start.date(),
                    window_end=(window.end - timedelta(days=1)).date(),
                    title=None,
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )

    # ── When agents run (heatmap) ────────────────────────────────
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "When agents run",
                "Token volume by hour (UTC) × weekday",
            ),
            unsafe_allow_html=True,
        )
        st.plotly_chart(
            dashboard_charts.hour_weekday_heatmap(heatmap_rows),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )

    # ── Recent agent runs expander ───────────────────────────────
    with st.expander("Recent agent runs", expanded=False):
        if agent_exits:
            df_exits = pd.DataFrame([
                {
                    "ts": r.ts,
                    "repo": r.repo,
                    "issue": r.issue,
                    "stage": r.stage,
                    "agent": r.agent_role,
                    "backend": r.backend,
                    "duration (s)": r.duration_s,
                    "exit": r.exit_code,
                    "timed out": r.timed_out,
                    "round": r.review_round,
                    "retry": r.retry_count,
                    "input tokens": r.input_tokens,
                    "output tokens": r.output_tokens,
                    "cost (USD)": r.cost_usd,
                    "cost source": r.cost_source,
                }
                for r in agent_exits
            ])
            st.dataframe(df_exits, use_container_width=True)
        else:
            st.info("No `agent_exit` rows match the current filters.")

    _render_drilldown(
        st=st,
        pd=pd,
        window=window,
        repo_filter=repo_filter,
        issue_input_parsed=issue_input_parsed,
        event_filter=event_filter,
        stage_filter=stage_filter,
    )

    st.markdown(
        '<div class="orch-foot">'
        f'Real data · window {window.start.date().isoformat()} → '
        f'{(window.end - timedelta(days=1)).date().isoformat()} · '
        f'{theme.fmt_num(summary.total_agent_runs)} agent runs'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_drilldown(
    *,
    st: Any,
    pd: Any,
    window: DateWindow,
    repo_filter: Optional[str],
    issue_input_parsed: Optional[int],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
) -> None:
    """Per-issue event trace section.

    Renders only when the operator typed a parseable issue number;
    when a repo is not also selected, surfaces an instructive notice
    so the empty result is not confused for a bug. Failures from the
    read model are caught and surfaced inline -- a drill-down error
    must not poison the overview the operator already scrolled past.
    """
    if issue_input_parsed is None:
        return
    st.subheader(f"Issue #{issue_input_parsed} drill-down")
    if repo_filter is None:
        st.info(
            "Pick a specific repo in the sidebar before drilling "
            "into an issue number -- GitHub issue numbers repeat "
            "across repos."
        )
        return
    try:
        with analytics_read.analytics_connection() as conn:
            trace = analytics_read.get_issue_events(
                repo=repo_filter,
                issue=issue_input_parsed,
                start=window.start,
                end=window.end,
                events=list(event_filter) if event_filter is not None else None,
                stages=list(stage_filter) if stage_filter is not None else None,
                conn=conn,
            )
    except analytics_read.AnalyticsReadError as e:
        st.error(f"Issue drill-down failed: {e}")
        return
    if trace:
        st.dataframe(
            pd.DataFrame([
                {
                    "ts": ev.ts,
                    "event": ev.event,
                    "stage": ev.stage,
                    "duration (s)": ev.duration_s,
                    "result": ev.result,
                    "agent": ev.agent_role,
                    "backend": ev.backend,
                    "exit": ev.exit_code,
                    "cost (USD)": ev.cost_usd,
                }
                for ev in trace
            ]),
            use_container_width=True,
        )
    else:
        st.info(
            f"No analytics events recorded for "
            f"`{repo_filter}#{issue_input_parsed}` "
            "under the current filters."
        )


if __name__ == "__main__":
    main()
