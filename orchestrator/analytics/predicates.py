# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Window / filter predicate builders shared by the read helpers.

Compose the `WHERE` clause (and its bound parameters) for the
date / repo / events / stages / issue filter shape every dashboard
read accepts. `_build_window_where` targets the base `analytics_events`
table, `_build_view_window_where` drops the `events` clause for the
`analytics_agent_runs` view (whose `event = 'agent_exit'` predicate is
baked in), and `_build_rollup_window_where` translates the `ts` window
onto the `analytics_daily_rollup` materialized view's `day` column.
`_agent_event_excluded` is the companion short-circuit the view-backed
readers call so the event-filter contract is honored before any SQL is
built.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class _WindowFilters:
    """The common window and selection filters accepted by readers."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    repo: Optional[str] = None
    events: Optional[Sequence[str]] = None
    stages: Optional[Sequence[str]] = None
    issue: Optional[int] = None

    def without_events(self) -> _WindowFilters:
        """Return filters suitable for a view with no `event` column."""
        return replace(self, events=None)

    def catalog_scope(self) -> _WindowFilters:
        """Return the date/repo subset valid for repo-level catalog rows."""
        return replace(self, events=None, stages=None, issue=None)


@dataclass
class _WhereBuilder:
    """Accumulate one parameterized SQL predicate and its values."""

    conditions: list[str] = field(default_factory=list)
    bindings: list[Any] = field(default_factory=list)

    def add_scalar(
        self,
        column: str,
        operand: Any,
        *,
        operator: str = "=",
    ) -> None:
        if operand is None:
            return
        self.conditions.append(f"{column} {operator} %s")
        self.bindings.append(operand)

    def add_selection(
        self,
        column: str,
        selection: Optional[Sequence[str]],
    ) -> None:
        if selection is None:
            return
        if not selection:
            self.conditions.append("FALSE")
            return
        placeholders = ", ".join(["%s"] * len(selection))
        self.conditions.append(f"{column} IN ({placeholders})")
        self.bindings.extend(selection)

    def render(self) -> tuple[str, list[Any]]:
        if not self.conditions:
            return "", self.bindings
        where_clause = " AND ".join(self.conditions)
        return f" WHERE {where_clause}", self.bindings


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
        int(filters.issue) if filters.issue is not None else None,
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


def _append_where_condition(where: str, condition: str) -> str:
    """Add a required condition after an optional generated predicate."""
    if where:
        return f"{where} AND {condition}"
    return f" WHERE {condition}"


def _prepend_where_condition(where: str, condition: str) -> str:
    """Add a required condition before an optional generated predicate."""
    if where:
        return f" WHERE {condition} AND {where.removeprefix(' WHERE ')}"
    return f" WHERE {condition}"


def _agent_event_excluded(events: Optional[Sequence[str]]) -> bool:
    """True when the active event filter excludes `agent_exit` rows.

    Functions that query `analytics_agent_runs` cannot push an
    `event IN (...)` clause down into the SQL (the view has no
    `event` column -- it filters internally to `event='agent_exit'`).
    They preserve the dashboard's event-filter contract by calling
    this helper up front and short-circuiting to an empty result:

    - ``None`` -> not excluded (no event filter at all).
    - non-empty sequence that lacks ``"agent_exit"`` -> excluded.
    - empty sequence (the cleared-multiselect signal) -> excluded.

    Keeps the agent-run aggregates in lockstep with `get_summary`
    et al. when the operator clears or narrows the events filter.
    """
    if events is None:
        return False
    if not events:
        return True
    return "agent_exit" not in events


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
