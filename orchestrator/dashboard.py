# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit analytics dashboard.

Interactive view over the `analytics_events` Postgres table populated
by `orchestrator.analytics.sync`. The page renders an analysis-first
layout (#317) over the redesigned read model: a sidebar with
data-extent-bounded `7d` / `30d` / `All` presets (plus a free date
range), computed insight banners, a KPI row with previous-window
deltas, the hero usage-over-time chart, the stage + review-round
breakdowns, the most-expensive issues table, per-backend efficiency
cards, the cost-source coverage bar, the per-repo activity chart, the
reliability / throughput chart, the weekday x hour activity heatmap,
an empty-window guard, the existing per-issue drill-down, and
DB-unset / read-error states. Reads go through
`orchestrator.analytics.read` (which already handles unset DB,
connection errors, and lazy psycopg import) and are wrapped in
`st.cache_data` keyed by `(start, end, repo, events, stages, issue)`
so every widget sees the same window.

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

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

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

DEFAULT_WINDOW_DAYS = 30
DEFAULT_RECENT_AGENT_EXITS = 100
DEFAULT_ISSUE_ROWS = 200
DEFAULT_EXPENSIVE_LIMIT = 10

# Sidebar window presets. `Custom` keeps the legacy two-date picker so
# the operator can pin an arbitrary window inside the data extent.
PRESET_7D = "7d"
PRESET_30D = "30d"
PRESET_ALL = "All"
PRESET_CUSTOM = "Custom"
PRESET_OPTIONS: tuple[str, ...] = (PRESET_7D, PRESET_30D, PRESET_ALL, PRESET_CUSTOM)
PRESET_LABELS: dict[str, str] = {
    PRESET_7D: "Last 7 days",
    PRESET_30D: "Last 30 days",
    PRESET_ALL: "All time",
    PRESET_CUSTOM: "Custom range",
}
PRESET_DAYS: dict[str, int] = {PRESET_7D: 7, PRESET_30D: 30}
DEFAULT_PRESET = PRESET_30D

# Insight thresholds. Centralized so a future tuning pass changes the
# behaviour in one place rather than across the page renderer.
FAILURE_RATE_BANNER_THRESHOLD = 0.10
COST_DELTA_BANNER_THRESHOLD = 0.25
UNPRICED_COVERAGE_THRESHOLD = 0.10
# `cost_source` values that mean "the parser could not produce a
# priced number". `unknown-price` is the documented signal from
# `orchestrator.usage`; `unknown` is what the read model substitutes
# when the column is NULL. Keep them both visible.
UNPRICED_COST_SOURCES: frozenset[str] = frozenset({"unknown-price", "unknown"})

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
    """Inclusive-start, exclusive-end datetime window.

    Matches the convention used across `analytics_read` (`start` is
    `ts >= %s`, `end` is `ts < %s`) so the dashboard's day-boundary
    pickers map cleanly to the SQL.
    """

    start: datetime
    end: datetime


@dataclass(frozen=True)
class InsightBanner:
    """A single banner line displayed at the top of the page.

    `severity` is one of `success` / `info` / `warning` / `error`; the
    Streamlit renderer maps each to the matching `st.<severity>` call.
    Keeping severity a plain string (rather than an Enum) means the
    helpers stay importable without Streamlit and the tests can
    compare against string literals.
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
    """Convert inclusive `[start_date, end_date]` to a `DateWindow`.

    The end-of-day boundary is computed as `end_date + 1 day` at
    midnight UTC so the read model's exclusive `ts < %s` includes
    every event from `end_date`. A user who picks `end < start` in
    Streamlit's two-date input gets the same window as the
    swapped-input case rather than an empty result -- typing the
    end date first is the common ordering mistake.
    """
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
    old still surfaces the last week of recorded activity. Returns
    `None` when the extent is empty (no events yet) or `preset` is
    `Custom` (the caller renders a date-range picker instead). An
    unknown preset string also returns `None`.

    For `7d` / `30d`, the start date is clamped to
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
    """Window of the same length ending at `window.start`.

    Used to compute previous-period KPI deltas: the operator sees how
    cost / events / agent runs trended versus the immediately
    preceding window of the same length. No data-extent clamping --
    callers that need it can intersect after the fact; for the KPI
    delta path an empty previous window produces a None delta via
    `kpi_delta`, which is the right behaviour.
    """
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
    """Lenient `#123` / `123` parser for the drill-down input.

    Returns `None` for empty / whitespace / `#`-only input, anything
    non-numeric, and non-positive integers. GitHub issue numbers
    start at 1, so `0` is invalid input rather than a meaningful
    drill-down target.
    """
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
    """Single source of truth for the "no DB configured" banner.

    Returns the user-facing string when `ANALYTICS_DB_URL` is unset
    (or set to one of the disable sentinels `off` / `disabled` /
    `none`, which `analytics.ANALYTICS_DB_URL` already collapses to
    `None`). Returns `None` when the URL is configured so the caller
    can branch on the optional cleanly.
    """
    if not analytics.ANALYTICS_DB_URL:
        return UNCONFIGURED_DB_MESSAGE
    return None


def resolve_stage_filter(
    selected: Sequence[str],
    available: Sequence[str],
) -> Optional[list[str]]:
    """Resolve the sidebar stage multiselect into a read-model filter.

    The multiselect defaults to every entry in `options.stages`,
    which `analytics_read.get_filter_options` populates from a
    `SELECT DISTINCT stage ... WHERE stage IS NOT NULL` -- so the
    "all selected" default lists every non-null stage but says
    nothing about rows whose `stage` column is NULL. Passing the
    full list through `_build_window_where` emits
    `stage IN (...)`, which silently excludes those NULL-stage
    rows -- a legitimate case (`stage_evaluation` records for
    issues with no workflow label, see
    `orchestrator/analytics/__init__.py`). So the dashboard maps:

    - no available options at all -> `None` (no SQL stage
      predicate; NULL-stage rows included);
    - user's selection equals the full set -> `None` (same
      rationale: this is the untouched default and the operator
      should see every row in the window);
    - an explicitly cleared multiselect (empty selection but
      options exist) -> `[]` (the read model encodes this as
      `FALSE` so no rows match -- the reviewer's documented
      "show nothing for this dimension" signal);
    - a proper subset -> that list (parameterised `IN (...)`).
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
    """Hashable cache key for the dashboard's window-scoped reads.

    Streamlit's `st.cache_data` keys cached results by the function's
    arguments, so the wrapper needs hashable values. Lists from the
    multiselect become tuples; `None` is preserved so the read
    model's tri-state (`None` = no filter, `[]` = empty / FALSE,
    non-empty = `IN (...)`) survives caching. Every widget that
    queries the read model uses this exact tuple shape so a single
    filter change invalidates every cached query in lockstep.
    """
    events_t = tuple(events) if events is not None else None
    stages_t = tuple(stages) if stages is not None else None
    return (window.start, window.end, repo, events_t, stages_t, issue)


def compute_insights(
    summary: Summary,
    *,
    prev_summary: Optional[Summary] = None,
    cost_coverage_rows: Sequence[CostCoverageRow] = (),
) -> list[InsightBanner]:
    """Banner lines surfaced at the top of the analysis-first page.

    Each banner is a single observation the operator should act on:

    - Failure rate exceeds `FAILURE_RATE_BANNER_THRESHOLD`: agent
      runs are exiting non-zero more than 10 % of the time.
    - Cost trend exceeds `COST_DELTA_BANNER_THRESHOLD` versus the
      previous window: a sustained 25 % swing is worth surfacing
      even though the KPI row already shows the delta.
    - Unpriced cost coverage exceeds `UNPRICED_COVERAGE_THRESHOLD`:
      the pricing table in `orchestrator.usage` is missing SKUs the
      parser is seeing in the wild. `unknown-price` is the
      documented signal; `unknown` (NULL `cost_source`) is bucketed
      alongside since both shapes lack a priced number.

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


def top_expensive_issues(
    rows: Sequence[IssueSummaryRow],
    *,
    limit: int = DEFAULT_EXPENSIVE_LIMIT,
) -> list[IssueSummaryRow]:
    """Issues sorted by total cost desc for the "where did spend go" table.

    Ties break on event count (busier issues first) then on the
    `(repo, issue)` pair so the order is deterministic when no cost
    information is available. Issues whose `total_cost_usd` is
    `None` (no agent-run rows in the window) sort to the end.
    `limit <= 0` returns an empty list, matching the read model's
    convention.
    """
    if limit <= 0:
        return []

    def _key(r: IssueSummaryRow) -> tuple:
        cost = r.total_cost_usd if r.total_cost_usd is not None else -1.0
        return (-cost, -int(r.event_count), r.repo, int(r.issue))

    return sorted(rows, key=_key)[:limit]


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit, pandas, plotly, the chart builders, and the
    theme tokens lazily so the orchestrator polling path never pulls
    them in. Run via `streamlit run orchestrator/dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    from orchestrator import dashboard_charts, dashboard_theme as theme

    st.set_page_config(
        page_title="Orchestrator analytics",
        layout="wide",
    )
    st.title("Orchestrator analytics")

    unset = db_unconfigured_message()
    if unset:
        st.warning(unset)
        st.stop()

    try:
        extent = analytics_read.get_data_extent()
        options = analytics_read.get_filter_options()
    except analytics_read.AnalyticsReadError as e:
        st.error(
            "Could not load analytics filter options: "
            f"{e}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()

    if extent.min_ts is None or extent.max_ts is None:
        st.info(NO_DATA_MESSAGE)
        st.stop()

    extent_min_d = extent.min_ts.date()
    extent_max_d = extent.max_ts.date()

    with st.sidebar:
        st.header("Filters")
        preset = st.radio(
            "Window",
            options=PRESET_OPTIONS,
            index=PRESET_OPTIONS.index(DEFAULT_PRESET),
            format_func=lambda p: PRESET_LABELS[p],
        )
        if preset == PRESET_CUSTOM:
            default_window = (
                preset_window(PRESET_30D, extent)
                or to_window(extent_min_d, extent_max_d)
            )
            start_date = st.date_input(
                "Start date",
                value=default_window.start.date(),
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
            end_date_default = (
                default_window.end - timedelta(days=1)
            ).date()
            end_date = st.date_input(
                "End date",
                value=end_date_default,
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
            window = to_window(start_date, end_date)
        else:
            preset_w = preset_window(preset, extent)
            assert preset_w is not None  # extent already validated
            window = preset_w
            st.caption(
                f"{window.start.date().isoformat()} → "
                f"{(window.end - timedelta(days=1)).date().isoformat()}"
            )

        repo_options = ("All", *options.repos) if options.repos else ("All",)
        repo_choice = st.selectbox("Repo", repo_options, index=0)
        event_choice = st.multiselect(
            "Events",
            list(options.events),
            default=list(options.events),
            help=(
                "Narrows every widget below. An empty selection means "
                "'show nothing for these events' -- clear the multiselect "
                "to confirm a dimension is empty."
            ),
        )
        stage_choice = st.multiselect(
            "Stages",
            list(options.stages),
            default=list(options.stages),
            help=(
                "Narrows every widget below. An empty selection means "
                "'show nothing for these stages'."
            ),
        )
        issue_input = st.text_input(
            "Issue number",
            value="",
            help=(
                "Enter `123` or `#123` to narrow every widget to one "
                "issue AND render the per-issue event trace at the "
                "bottom. Requires a specific repo above -- GitHub "
                "issue numbers repeat across repos."
            ),
        )

    repo_filter = None if repo_choice == "All" else repo_choice
    issue_input_parsed = parse_issue_number(issue_input)
    # The issue input is a real filter only when a single repo is
    # selected (because issue numbers are not globally unique).
    # Without a repo selection it stays inert at the SQL layer, and
    # the drill-down section renders an instructive notice.
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

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_summary(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_summary(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_time_series(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_time_series(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_stage_breakdown(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_stage_breakdown(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_recent_agent_exits(
        start, end, repo, events_t, stages_t, issue
    ):
        return analytics_read.get_recent_agent_exits(
            limit=DEFAULT_RECENT_AGENT_EXITS,
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_issues(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_issues(
            limit=DEFAULT_ISSUE_ROWS,
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_review_round(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_review_round_breakdown(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_efficiency(
        start, end, repo, events_t, stages_t, issue
    ):
        return analytics_read.get_backend_efficiency(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_repo_breakdown(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_repo_breakdown(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_cost_coverage(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_cost_coverage(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_hourly_heatmap(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_hourly_heatmap(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_throughput(start, end, repo, events_t, stages_t, issue):
        return analytics_read.get_throughput_breakdown(
            start=start, end=end, repo=repo,
            events=list(events_t) if events_t is not None else None,
            stages=list(stages_t) if stages_t is not None else None,
            issue=issue,
        )

    try:
        summary = _read_summary(*key)
        prev_summary = _read_summary(*prev_key)
        ts_points = _read_time_series(*key)
        stage_rows = _read_stage_breakdown(*key)
        agent_exits = _read_recent_agent_exits(*key)
        issues_rows = _read_issues(*key)
        review_round_rows = _read_review_round(*key)
        backend_rows = _read_backend_efficiency(*key)
        repo_rows = _read_repo_breakdown(*key)
        cost_coverage_rows = _read_cost_coverage(*key)
        heatmap_rows = _read_hourly_heatmap(*key)
        throughput_rows = _read_throughput(*key)
    except analytics_read.AnalyticsReadError as e:
        st.error(
            f"Analytics query failed: {e}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()

    # Empty-window guard: when nothing in the window matches the
    # filters, render the helper banner and skip the rest. The
    # drill-down still renders below so the operator can confirm the
    # issue number they typed is just out of range.
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
        st.subheader("Insights")
        for banner in banners:
            _emit_banner(st, banner)

    st.subheader("Overview")
    success_rate = (
        1.0
        - (summary.failed_agent_runs / summary.total_agent_runs)
        if summary.total_agent_runs > 0
        else None
    )
    prev_success_rate = (
        1.0
        - (prev_summary.failed_agent_runs / prev_summary.total_agent_runs)
        if prev_summary.total_agent_runs > 0
        else None
    )

    cols = st.columns(5)
    _metric_with_delta(
        cols[0], "Events", summary.total_events, prev_summary.total_events
    )
    _metric_with_delta(
        cols[1], "Issues", summary.distinct_issues, prev_summary.distinct_issues
    )
    _metric_with_delta(
        cols[2],
        "Agent runs",
        summary.total_agent_runs,
        prev_summary.total_agent_runs,
    )
    _metric_with_delta(
        cols[3],
        "Cost (USD)",
        summary.total_cost_usd,
        prev_summary.total_cost_usd,
        value_fmt=lambda v: f"${v:,.2f}",
    )
    if success_rate is None:
        cols[4].metric("Agent success rate", "—")
    else:
        delta_str = None
        if prev_success_rate is not None:
            delta_pct = (success_rate - prev_success_rate) * 100
            delta_str = f"{delta_pct:+.1f} pp"
        cols[4].metric(
            "Agent success rate",
            f"{success_rate * 100:.1f}%",
            delta=delta_str,
        )

    st.subheader("Activity")
    st.plotly_chart(
        dashboard_charts.usage_over_time(ts_points),
        use_container_width=True,
    )

    col_stage, col_round = st.columns(2)
    with col_stage:
        st.plotly_chart(
            dashboard_charts.stage_bars(stage_rows),
            use_container_width=True,
        )
    with col_round:
        st.plotly_chart(
            _review_round_bars_from_rows(
                go=go, theme=theme, rows=review_round_rows
            ),
            use_container_width=True,
        )

    st.subheader("Top issues by cost")
    expensive = top_expensive_issues(issues_rows)
    if expensive:
        df_expensive = pd.DataFrame([
            {
                "repo": r.repo,
                "issue": r.issue,
                "cost (USD)": r.total_cost_usd,
                "events": r.event_count,
                "agent exits": r.agent_exits,
                "failed runs": r.failed_agent_runs,
                "max review round": r.max_review_round,
                "input tokens": r.total_input_tokens,
                "output tokens": r.total_output_tokens,
                "latest stage": r.latest_stage,
                "last seen": r.last_seen,
            }
            for r in expensive
        ])
        st.dataframe(df_expensive, use_container_width=True)
    else:
        st.info("No agent runs with recorded cost in this window.")

    st.subheader("Backend efficiency")
    if backend_rows:
        backend_cols = st.columns(min(len(backend_rows), 4) or 1)
        for idx, row in enumerate(backend_rows):
            col = backend_cols[idx % len(backend_cols)]
            fail_pct = (
                (row.failed / row.runs) * 100 if row.runs > 0 else 0.0
            )
            duration_txt = (
                f"{row.avg_duration_s:.1f}s"
                if row.avg_duration_s is not None
                else "—"
            )
            col.metric(
                row.backend,
                f"{row.runs} runs",
                delta=(
                    f"{fail_pct:.0f}% failed"
                    if row.runs > 0
                    else None
                ),
                delta_color="inverse",
            )
            col.caption(
                f"avg {duration_txt} · "
                f"${row.total_cost_usd:,.2f}"
            )
    else:
        st.info("No `agent_exit` rows match the current filters.")

    cov_col, repo_col = st.columns(2)
    with cov_col:
        st.subheader("Cost source coverage")
        if cost_coverage_rows:
            st.plotly_chart(
                _cost_coverage_bar(
                    go=go, theme=theme, rows=cost_coverage_rows
                ),
                use_container_width=True,
            )
        else:
            st.info("No `agent_exit` rows match the current filters.")
    with repo_col:
        st.subheader("Activity by repo")
        if repo_rows:
            st.plotly_chart(
                _repo_chart_from_rows(
                    go=go, theme=theme, rows=repo_rows
                ),
                use_container_width=True,
            )
        else:
            st.info("No repos match the current filters.")

    st.subheader("Reliability / throughput")
    st.plotly_chart(
        _reliability_chart(
            go=go,
            theme=theme,
            ts_points=ts_points,
            throughput_rows=throughput_rows,
        ),
        use_container_width=True,
    )

    st.subheader("Activity by weekday × hour")
    st.plotly_chart(
        _heatmap_from_rows(go=go, theme=theme, rows=heatmap_rows),
        use_container_width=True,
    )

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


def _emit_banner(st: Any, banner: InsightBanner) -> None:
    """Render `banner` through the matching `st.<severity>` call."""
    fn = {
        "success": st.success,
        "info": st.info,
        "warning": st.warning,
        "error": st.error,
    }.get(banner.severity, st.info)
    fn(banner.message)


def _metric_with_delta(
    col: Any,
    label: str,
    current: float,
    previous: float,
    *,
    value_fmt=lambda v: f"{int(v):,}",
) -> None:
    """`st.metric` with a previous-window delta shown as a percentage.

    The delta is rendered as `"+12.3%"` / `"-4.0%"` rather than the
    absolute difference so the row reads uniformly across mixed
    columns (events vs cost vs token counts). When the previous
    window is zero (or negative) the delta is hidden -- a divide by
    zero would otherwise read as infinity.
    """
    delta = kpi_delta(float(current), float(previous))
    delta_str = None if delta is None else f"{delta * 100:+.1f}%"
    col.metric(label, value_fmt(current), delta=delta_str)


def _review_round_bars_from_rows(
    *, go: Any, theme: Any, rows: Sequence[Any]
) -> Any:
    """Bar chart from `ReviewRoundBucketRow` (totals + failed stacked).

    `dashboard_charts.review_round_bars` takes raw `AgentExitRow`s
    and counts by `review_round`; here we already have the
    aggregated bucket rows from the read model, so a parallel
    builder keeps both code paths first-class. The bucket label
    order is bounded (`0` / `1` / `2` / `3-5` / `6+` / `unknown`)
    so a deterministic ordering helps the chart stay readable.
    """
    if not rows:
        return _empty_plotly_figure(
            go=go, theme=theme,
            message="No `agent_exit` rows match the current filters.",
        )
    order = ("0", "1", "2", "3-5", "6+", "unknown")
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            order.index(r.bucket) if r.bucket in order else len(order)
        ),
    )
    buckets = [r.bucket for r in sorted_rows]
    succeeded = [int(r.runs) - int(r.failed) for r in sorted_rows]
    failed = [int(r.failed) for r in sorted_rows]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=buckets,
            y=succeeded,
            name="succeeded",
            marker_color=theme.SUCCESS,
            hovertemplate="round %{x}: %{y} succeeded<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=buckets,
            y=failed,
            name="failed",
            marker_color=theme.DANGER,
            hovertemplate="round %{x}: %{y} failed<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="stack",
        **theme.base_layout(title="Agent runs by review round"),
    )
    fig.update_xaxes(title_text="review round", type="category")
    fig.update_yaxes(title_text="agent runs")
    return fig


def _cost_coverage_bar(
    *, go: Any, theme: Any, rows: Sequence[CostCoverageRow]
) -> Any:
    """Horizontal bar of `cost_source` counts.

    The chart builder module exposes a donut variant; the analysis-
    first layout asked for a bar so the absolute counts read off the
    axis directly. Bars are sorted by count descending so the
    largest cohort sits at the top, and `unknown-price` / `unknown`
    are colored through the explicit `COST_SOURCE_COLORS` map.
    """
    ordered = sorted(
        rows, key=lambda r: (-int(r.runs), r.cost_source)
    )
    labels = [r.cost_source for r in ordered]
    fig = go.Figure(
        go.Bar(
            x=[int(r.runs) for r in ordered],
            # Plotly draws the first y-value at the bottom; reverse
            # so the largest cohort surfaces at the top of the chart.
            y=labels,
            orientation="h",
            marker_color=[
                theme.color_for(
                    lbl, labels, explicit=theme.COST_SOURCE_COLORS
                )
                for lbl in labels
            ],
            hovertemplate="%{y}: %{x} runs<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title="Cost source coverage"))
    fig.update_xaxes(title_text="agent runs")
    fig.update_yaxes(autorange="reversed")
    return fig


def _repo_chart_from_rows(
    *, go: Any, theme: Any, rows: Sequence[Any]
) -> Any:
    """Grouped bar of (issues, events, agent_exits) per repo.

    `dashboard_charts.repo_bars` aggregates from `IssueSummaryRow`s;
    the read model now exposes `get_repo_breakdown` directly, so
    rendering off the pre-aggregated rows avoids re-summing N issue
    rows on the dashboard side. Sorted by event count desc so the
    busiest repo is leftmost.
    """
    if not rows:
        return _empty_plotly_figure(
            go=go, theme=theme,
            message="No repos match the current filters.",
        )
    ordered = sorted(rows, key=lambda r: (-int(r.events), r.repo))
    repos = [r.repo for r in ordered]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=repos,
            y=[int(r.issues) for r in ordered],
            name="issues",
            marker_color=theme.PRIMARY,
            hovertemplate="%{x}: %{y} issues<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=repos,
            y=[int(r.events) for r in ordered],
            name="events",
            marker_color=theme.SECONDARY,
            hovertemplate="%{x}: %{y} events<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=repos,
            y=[int(r.agent_exits) for r in ordered],
            name="agent runs",
            marker_color=theme.SUCCESS,
            hovertemplate="%{x}: %{y} agent runs<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="group", **theme.base_layout(title="Activity by repo")
    )
    fig.update_yaxes(title_text="count")
    return fig


def _reliability_chart(
    *,
    go: Any,
    theme: Any,
    ts_points: Sequence[Any],
    throughput_rows: Sequence[Any],
) -> Any:
    """Daily activity bars overlaid with resolved / rejected throughput.

    The bars (left axis) are the per-day total event count -- the
    same shape `dashboard_charts.throughput` plots in isolation --
    and the two scatter traces (right axis) carry the resolved /
    rejected daily counts from `get_throughput_breakdown` so the
    operator can read "did volume actually translate into closed
    issues" off a single chart.
    """
    if not ts_points and not throughput_rows:
        return _empty_plotly_figure(
            go=go, theme=theme,
            message="No events match the current filters.",
        )
    daily_events: dict = {}
    for p in ts_points:
        daily_events[p.day] = daily_events.get(p.day, 0) + int(p.count)
    days = sorted(set(daily_events) | {r.day for r in throughput_rows})
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=days,
            y=[daily_events.get(d, 0) for d in days],
            name="events",
            marker_color=theme.PRIMARY,
            opacity=0.7,
            hovertemplate="%{x}: %{y} events<extra></extra>",
        )
    )
    resolved_by_day = {r.day: int(r.resolved) for r in throughput_rows}
    rejected_by_day = {r.day: int(r.rejected) for r in throughput_rows}
    fig.add_trace(
        go.Scatter(
            x=days,
            y=[resolved_by_day.get(d, 0) for d in days],
            name="resolved",
            mode="lines+markers",
            line={"color": theme.SUCCESS},
            yaxis="y2",
            hovertemplate="%{x}: %{y} resolved<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=days,
            y=[rejected_by_day.get(d, 0) for d in days],
            name="rejected",
            mode="lines+markers",
            line={"color": theme.DANGER},
            yaxis="y2",
            hovertemplate="%{x}: %{y} rejected<extra></extra>",
        )
    )
    layout = theme.base_layout(title="Reliability / throughput")
    layout["yaxis"] = {**layout.get("yaxis", {}), "title": {"text": "events"}}
    layout["yaxis2"] = {
        "title": {"text": "resolved / rejected"},
        "overlaying": "y",
        "side": "right",
        "gridcolor": theme.GRID,
        "linecolor": theme.GRID,
    }
    fig.update_layout(**layout)
    return fig


# Postgres `EXTRACT(DOW FROM ts)` is 0 = Sunday; Python's
# `datetime.weekday()` (and the rest of the orchestrator's analytics
# code) is 0 = Monday. Convert at the chart layer so the y-axis
# labels stay Monday-first.
_WEEKDAY_LABELS: tuple[str, ...] = (
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
)


def _heatmap_from_rows(
    *, go: Any, theme: Any, rows: Sequence[Any]
) -> Any:
    """7x24 heatmap built from `HourlyHeatmapPoint` rows."""
    matrix = [[0] * 24 for _ in range(7)]
    for p in rows:
        # PG DOW: 0=Sun..6=Sat; Python weekday: 0=Mon..6=Sun.
        py_weekday = (int(p.weekday) + 6) % 7
        hour = int(p.hour)
        if 0 <= py_weekday < 7 and 0 <= hour < 24:
            matrix[py_weekday][hour] = int(p.count)
    fig = go.Figure(
        go.Heatmap(
            z=matrix,
            x=[f"{h:02d}" for h in range(24)],
            y=list(_WEEKDAY_LABELS),
            colorscale="Blues",
            hovertemplate="%{y} %{x}:00 -- %{z} events<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title="Activity by weekday × hour"))
    fig.update_xaxes(title_text="hour (UTC)", type="category")
    fig.update_yaxes(title_text="weekday", autorange="reversed")
    if not rows:
        fig.add_annotation(
            text="No events match the current filters.",
            x=0.5, y=0.5,
            xref="paper", yref="paper",
            showarrow=False,
            font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
        )
    return fig


def _empty_plotly_figure(*, go: Any, theme: Any, message: str) -> Any:
    """Centered empty-state annotation on a blank Plotly figure."""
    fig = go.Figure()
    fig.update_layout(**theme.base_layout())
    fig.add_annotation(
        text=message, x=0.5, y=0.5,
        xref="paper", yref="paper",
        showarrow=False,
        font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


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
        trace = analytics_read.get_issue_events(
            repo=repo_filter,
            issue=issue_input_parsed,
            start=window.start,
            end=window.end,
            events=list(event_filter) if event_filter is not None else None,
            stages=list(stage_filter) if stage_filter is not None else None,
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
