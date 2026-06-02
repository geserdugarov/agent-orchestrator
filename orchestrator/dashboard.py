# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit analytics dashboard.

Interactive view over the `analytics_events` Postgres table
populated by `orchestrator.analytics.sync`. Reads run through
`orchestrator.analytics.read` (which already handles unset DB,
connection errors, and lazy psycopg import) so this module owns
only the UI shape: sidebar filter controls, high-level metrics, a
time-series chart, stage / event breakdowns, the recent-agent-run
table with cost / token columns, and per-issue drill-down.

Streamlit (and its transitive pandas) are imported *lazily* inside
`main()` so the polling tick's `orchestrator.*` import surface
stays free of the dashboard's dependency footprint. The module
loads without `streamlit` installed -- only `streamlit run
orchestrator/dashboard.py` (or a direct `main()` call) actually
materializes the imports. Tests for the pure helpers below do not
need Streamlit installed.

Run:
    uv sync --group dashboard
    uv run streamlit run orchestrator/dashboard.py

The finance-tracking Streamlit example informed the sidebar /
metrics / drill-down layout but none of its auth or
finance-specific logic carried over -- this module talks only to
the read model and Streamlit's stdlib primitives.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

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

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 30
DEFAULT_RECENT_AGENT_EXITS = 100
DEFAULT_ISSUE_ROWS = 200

UNCONFIGURED_DB_MESSAGE = (
    "`ANALYTICS_DB_URL` is not configured. Set it in your environment "
    "(see `.env.example.advanced` and `docs/configuration.md`) and "
    "reload the dashboard to view analytics."
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


def default_date_range(
    *,
    today: Optional[date] = None,
    days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[date, date]:
    """Default `[start, end]` inclusive date range for the sidebar.

    `today` injection keeps this testable; the production path
    relies on `date.today()`. `days` is clamped at 1 so `days=0`
    (an explicit "today only" choice) still returns `(today, today)`
    rather than a reversed range.
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


def main() -> None:
    """Streamlit entrypoint.

    Imports `streamlit` and `pandas` lazily so the orchestrator
    polling path (which loads `orchestrator.*` modules at process
    start) never pulls them in. Run via
    `streamlit run orchestrator/dashboard.py`; Streamlit invokes the
    script with `__name__ == "__main__"`, which falls through to the
    sentinel at the bottom of this file.
    """
    import pandas as pd
    import streamlit as st

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
        options = analytics_read.get_filter_options()
    except analytics_read.AnalyticsReadError as e:
        st.error(
            "Could not load filter options from the analytics database: "
            f"{e}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()

    with st.sidebar:
        st.header("Filters")
        default_start, default_end = default_date_range()
        start_date = st.date_input("Start date", default_start)
        end_date = st.date_input("End date", default_end)
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

    window = to_window(start_date, end_date)
    repo_filter = None if repo_choice == "All" else repo_choice
    issue_input_parsed = parse_issue_number(issue_input)
    # The issue input is a real filter only when a single repo is
    # selected (because issue numbers are not globally unique).
    # Without a repo selection it stays inert at the SQL layer, and
    # the drill-down section renders an instructive notice. This is
    # the "drill-down is explicit" path the reviewer asked for, while
    # event/stage stay as plain SQL filters that consistently narrow
    # everything.
    issue_filter = (
        issue_input_parsed if repo_filter is not None else None
    )

    # The event filter threads straight through: `event` is NOT NULL
    # in the schema, so always emitting an `event IN (...)` clause
    # for the user's selection (including the all-selected default)
    # is loss-free. An explicitly cleared multiselect becomes `[]`
    # which the read model turns into a `FALSE` predicate -- the
    # documented "show nothing for this dimension" signal.
    event_filter = list(event_choice)
    # The stage filter is asymmetric. `options.stages` is populated
    # from `SELECT DISTINCT stage ... WHERE stage IS NOT NULL`, so
    # the all-selected default lists every non-null stage but says
    # nothing about NULL-stage rows. Always emitting
    # `stage IN (...)` would silently drop `stage_evaluation` rows
    # whose issue had no workflow label. `resolve_stage_filter`
    # collapses the all-selected-and-no-options-available cases
    # back to `None` (no predicate, NULL stages included) while
    # preserving `[]` for an explicitly cleared multiselect.
    stage_filter = resolve_stage_filter(stage_choice, options.stages)

    read_kwargs = {
        "start": window.start,
        "end": window.end,
        "repo": repo_filter,
        "events": event_filter,
        "stages": stage_filter,
        "issue": issue_filter,
    }

    try:
        summary = analytics_read.get_summary(**read_kwargs)
        ts_points = analytics_read.get_time_series(**read_kwargs)
        stage_rows = analytics_read.get_stage_breakdown(**read_kwargs)
        event_rows = analytics_read.get_event_breakdown(**read_kwargs)
        agent_exits = analytics_read.get_recent_agent_exits(
            limit=DEFAULT_RECENT_AGENT_EXITS, **read_kwargs
        )
        issues_rows = analytics_read.get_issues(
            limit=DEFAULT_ISSUE_ROWS, **read_kwargs
        )
    except analytics_read.AnalyticsReadError as e:
        st.error(
            f"Analytics query failed: {e}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()

    st.subheader("Overview")
    cols = st.columns(5)
    cols[0].metric("Events", f"{summary.total_events:,}")
    cols[1].metric("Issues", f"{summary.distinct_issues:,}")
    cols[2].metric("Repos", f"{summary.distinct_repos:,}")
    cols[3].metric("Cost (USD)", f"${summary.total_cost_usd:,.2f}")
    cols[4].metric(
        "Tokens in / out",
        f"{summary.total_input_tokens:,} / {summary.total_output_tokens:,}",
    )

    st.subheader("Events over time")
    if ts_points:
        df_ts = pd.DataFrame(
            [{"day": p.day, "event": p.event, "count": p.count}
             for p in ts_points]
        )
        pivot = df_ts.pivot_table(
            index="day", columns="event", values="count", fill_value=0
        )
        st.bar_chart(pivot)
    else:
        st.info("No events match the current filters.")

    col_stage, col_event = st.columns(2)
    with col_stage:
        st.subheader("By stage")
        if stage_rows:
            st.dataframe(
                pd.DataFrame([
                    {"stage": r.stage, "count": r.count,
                     "avg duration (s)": r.avg_duration_s}
                    for r in stage_rows
                ]),
                use_container_width=True,
            )
        else:
            st.info("No stage data matches the current filters.")
    with col_event:
        st.subheader("By event")
        if event_rows:
            st.dataframe(
                pd.DataFrame([
                    {"event": r.event, "count": r.count}
                    for r in event_rows
                ]),
                use_container_width=True,
            )
        else:
            st.info("No event data matches the current filters.")

    st.subheader("Recent agent runs")
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

    st.subheader("Issues")
    if issues_rows:
        df_issues = pd.DataFrame([
            {
                "repo": r.repo,
                "issue": r.issue,
                "events": r.event_count,
                "first seen": r.first_seen,
                "last seen": r.last_seen,
                "latest stage": r.latest_stage,
                "agent exits": r.agent_exits,
                "cost (USD)": r.total_cost_usd,
                "input tokens": r.total_input_tokens,
                "output tokens": r.total_output_tokens,
            }
            for r in issues_rows
        ])
        st.dataframe(df_issues, use_container_width=True)
    else:
        st.info("No issues match the current filters.")

    if issue_input_parsed is not None:
        st.subheader(f"Issue #{issue_input_parsed} drill-down")
        # GitHub issue numbers are only unique within a repo, so refuse
        # to guess a target when the user left the repo filter on "All".
        if repo_filter is None:
            st.info(
                "Pick a specific repo in the sidebar before drilling "
                "into an issue number -- GitHub issue numbers repeat "
                "across repos."
            )
        else:
            try:
                trace = analytics_read.get_issue_events(
                    repo=repo_filter,
                    issue=issue_input_parsed,
                    start=window.start,
                    end=window.end,
                    events=event_filter,
                    stages=stage_filter,
                )
            except analytics_read.AnalyticsReadError as e:
                st.error(f"Issue drill-down failed: {e}")
                trace = []
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
