# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Window predicate construction for analytics tables and views."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from orchestrator.analytics._predicate_models import _WhereBuilder, _WindowFilters


def _day_bound(bound: Optional[datetime]) -> Any:
    if isinstance(bound, datetime):
        return bound.date()
    return bound


def _build_where(
    filters: _WindowFilters,
    *,
    time_column: str,
    day_bounds: bool,
) -> tuple[str, list[Any]]:
    """Build a base-table or daily-rollup predicate from common filters."""
    builder = _WhereBuilder()
    start = _day_bound(filters.start) if day_bounds else filters.start
    end = _day_bound(filters.end) if day_bounds else filters.end
    builder.add_scalar(time_column, start, operator=">=")
    builder.add_scalar(time_column, end, operator="<")
    builder.add_scalar("repo", filters.repo)
    builder.add_scalar(
        "issue",
        None if filters.issue is None else int(filters.issue),
    )
    builder.add_selection("event", filters.events)
    builder.add_selection("stage", filters.stages)
    return builder.render()


def _build_window_where(
    filters: _WindowFilters,
) -> tuple[str, list[Any]]:
    """Compose the shared `WHERE` clause for window-scoped queries.

    Returns (clause, params); the clause includes a leading `WHERE`
    when at least one filter is set, or an empty string otherwise.
    Callers concatenate this directly into their SQL so the same
    filter shape (start / end / repo / events / stages / issue) is
    available across every aggregate.

    ``events`` / ``stages`` distinguish three cases on purpose:

    - ``None`` (the default) means "no filter on this column" --
      every row is eligible. This is what dashboard callers pass
      when the user has not interacted with the multiselect.
    - A non-empty sequence emits a parameterised ``IN (...)``
      clause -- the dashboard sends the user's selected subset.
    - An empty sequence emits a tautologically-false predicate
      (``FALSE``) so the query returns no rows. The dashboard
      treats a cleared multiselect as "show nothing for this
      dimension" rather than the previous "show everything"
      behavior; encoding that as SQL is what makes summary /
      time-series / breakdown / agent-run / issues counts move
      together when the operator drags a filter to empty.

    ``issue`` narrows to a single GitHub issue number. GitHub issue
    numbers are only unique within a repo, so the dashboard refuses
    to apply this filter when ``repo`` is not also set; the helper
    itself does not enforce that -- it just emits the predicate.
    """
    return _build_where(filters, time_column="ts", day_bounds=False)


def _build_view_window_where(
    filters: _WindowFilters,
) -> tuple[str, list[Any]]:
    """`_build_window_where` minus the ``events`` clause.

    Use against `analytics_agent_runs` queries. Callers must have
    already short-circuited on `_agent_event_excluded(events)` so
    the event-filter contract is honored before the SQL is built.
    """
    return _build_window_where(filters.without_events())


_DAILY_ROLLUP_VIEW = "analytics_daily_rollup"


def _build_rollup_window_where(
    filters: _WindowFilters,
) -> tuple[str, list[Any]]:
    """`_build_window_where` translated to the rollup's `day` column.

    The materialized view `analytics_daily_rollup` is keyed on
    `(day, repo, issue, event, stage, backend, cost_source)` with
    `day = (ts AT TIME ZONE 'UTC')::date`, so a `ts`-bounded window
    becomes a `day`-bounded one. The dashboard's `to_window` produces
    midnight-aligned `[start, end)` UTC datetimes; for those the
    rollup is semantically equivalent to a `ts`-scoped scan because
    every event in `[start_day, end_day)` lands on exactly one rollup
    row. Sub-day-aligned bounds collapse to day granularity (the
    rollup carries no finer resolution) -- the dashboard never passes
    those, so this is documentation rather than a runtime guard.

    ``events`` / ``stages`` semantics mirror `_build_window_where`:
    ``None`` is no filter, a non-empty sequence is parameterised
    ``IN (...)``, and an empty sequence emits a tautologically-false
    predicate so the cleared-multiselect signal still drops to zero.
    """
    return _build_where(filters, time_column="day", day_bounds=True)
