# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Core analytics read result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class FilterOptions:
    """Distinct values for the dashboard filter dropdowns.

    Tuples (not lists) so the result is hashable and obviously
    immutable to callers that cache it. Empty tuples are the
    documented "DB unset" and "empty table" result -- the dashboard
    should render a disabled filter rather than crash.
    """

    repos: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    agent_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataExtent:
    """Earliest and latest event timestamps in the table.

    The dashboard uses this to default the sidebar date picker to a
    window that actually contains data -- a freshly-deployed database
    has no rows, so picking today's date returns nothing. Both fields
    are `None` when the table is empty or when `ANALYTICS_DB_URL` is
    unset; the dashboard branches on that to render a "no data yet"
    state.
    """

    min_ts: Optional[datetime] = None
    max_ts: Optional[datetime] = None


@dataclass(frozen=True)
class Summary:
    """Aggregate counts for a date-bounded window.

    Zero-valued by default so the "DB unset" path can return
    ``Summary()`` and the dashboard renders a still-meaningful page.
    `by_event` and `by_stage` use plain dicts because Streamlit-style
    rendering iterates them; ordering follows the SQL `GROUP BY` so
    the dashboard sees stable counts even if the rows reshuffle
    between queries. `total_agent_runs` / `failed_agent_runs` count
    `event = 'agent_exit'` rows (and the failing subset where
    `exit_code <> 0`) inside the same filtered window so the
    dashboard's "agent success rate" reads off the same query as the
    rest of the overview. `total_cache_read_tokens` /
    `total_cache_write_tokens` carry the cache-band tokens the
    redesigned dashboard's "Total tokens" KPI and sparkline include
    in the headline figure (the standalone mock's total is
    ``input + output + cache_read + cache_write``).
    """

    total_events: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = field(default_factory=float)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_agent_runs: int = 0
    failed_agent_runs: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    # Window-wide timeout count -- agent_exit rows whose `timed_out`
    # flag is true. Sourced from the totals query so the redesigned
    # reliability "Timeouts" tile sees every timed-out run in the
    # window, not just the latest N from `get_recent_agent_exits`.
    timed_out_agent_runs: int = 0


@dataclass(frozen=True)
class TimeSeriesPoint:
    """One (day, event, count) cell of the daily time-series.

    `day` is a `date`, not a `datetime`, because the SQL aggregates
    over `date_trunc('day', ts)` and a date matches a Plotly chart's
    axis directly. The cell carries the per-event cost / token
    aggregates as well so a "spend over time" chart can pivot off the
    same query the activity chart uses -- avoids a second round trip
    for what is already grouped by `(day, event)`. Cache-band tokens
    surface alongside input / output so the redesigned hero chart's
    `mode="type"` stack can render an Input / Output / Cache stack
    instead of dropping cache tokens on the floor. Fields default to
    zero so a fake-cursor fixture that returns just `(day, event,
    count)` rows still validates the no-aggregate path.
    """

    day: date
    event: str
    count: int
    cost_usd: float = field(default_factory=float)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
