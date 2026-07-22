# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)
from tests.analytics_assertions import (
    assert_row_fields,
    assert_sql_fragments,
)

_WINDOW_END_DAY = 28
_TS_DAY_DAY = 25
_TS_NEXT_DAY_DAY = 26
_AGGREGATES_BREAKDOWNS_TOTAL_EVENTS = 42
_AGGREGATES_BREAKDOWNS_TOTAL_COST_USD = 1.234
_AGGREGATES_BREAKDOWNS_TOTAL_OUTPUT_TOKENS = 200
_AGENT_RUN_COLUMNS_TOTAL_AGENT_RUNS = 15
_CACHE_TOKEN_COLUMNS_TOTAL_CACHE_READ_TOKEN = 1200
_CACHE_TOKEN_COLUMNS_TOTAL_CACHE_WRITE_TOKE = 800
_DAY_NORMALISED_DATE_DAY = 25
_AGGREGATES_ROUND_TRIP_COST_USD = 0.42
_AGGREGATES_ROUND_TRIP_OUTPUT_TOKENS = 500
_AGGREGATES_ROUND_TRIP_CACHE_READ_TOKENS = 200

# The combined summary query opens with this CTE and scans the daily
# rollup; both fragments are asserted against the emitted SQL across
# the module. `get_kpi_prev` emits the trimmed scalar SELECT instead.
_WIN_CTE = "WITH win AS"
_ROLLUP_SCAN = "FROM analytics_daily_rollup"
_SCALARS_CTE = "AS total_cost_usd"
_KIND_TOTALS = "t"
_AGENT_EXIT = "agent_exit"
_STAGE_ENTER = "stage_enter"

_REPO = "owner/repo"
_REPO_SHORT = "owner/r"

# Reused window bounds; the rollup cutover binds their `.date()`
# projection, so the day components are the assertion surface rather
# than incidental fixture noise.
_YEAR = 2026
_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 5, _WINDOW_END_DAY, tzinfo=timezone.utc)
_PREV_START = datetime(_YEAR, 4, 1, tzinfo=timezone.utc)
_TS_DAY = date(_YEAR, 5, _TS_DAY_DAY)
_TS_NEXT_DAY = date(_YEAR, 5, _TS_NEXT_DAY_DAY)


class SummaryTest(unittest.TestCase):
    """Date-bounded aggregate counts plus per-event / per-stage
    breakdowns. Empty results give a zero-valued Summary, not None."""

    def test_returns_zero_summary_when_db_url_unset(self) -> None:
        analytics_read = _reload_read(db_url="")
        connected = []
        summary = analytics_read.get_summary(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(summary, analytics_read.Summary())

    def test_empty_rows_yield_zero_summary(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # No rows from the unioned SELECT (a fake that emits nothing).
        conn.rows_for = {}
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary, analytics_read.Summary())

    def test_aggregates_and_breakdowns(self) -> None:
        # Layer 3 collapses totals + by_event + by_stage into one
        # UNION-ALL'd query keyed by a `kind` discriminator. Each
        # row carries the 13-column shape; the by_event / by_stage
        # rows only populate `kind`, `label`, and `count_val`, with
        # trailing NULLs that the reader ignores. The fixture emits
        # the breakdown pairs in arbitrary order so the in-Python
        # `COUNT DESC, label ASC` sort that preserves the previous
        # SQL ordering is exercised.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _WIN_CTE: [
                (_KIND_TOTALS, None, 42, 10, 2, 1.234, 100, 200, 0, 0, 0, 0, 0),
                ("e", _AGENT_EXIT, 12, None, None, None, None, None, None, None, None, None, None),
                ("e", _STAGE_ENTER, 30, None, None, None, None, None, None, None, None, None, None),
                ("s", "validating", 10, None, None, None, None, None, None, None, None, None, None),
                ("s", "implementing", 20, None, None, None, None, None, None, None, None, None, None),
            ],
        }
        summary = analytics_read.get_summary(
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO,
            connect=conn.as_connect,
        )
        self.assertEqual(summary.total_events, _AGGREGATES_BREAKDOWNS_TOTAL_EVENTS)
        self.assertEqual(summary.distinct_issues, 10)
        self.assertEqual(summary.distinct_repos, 2)
        self.assertEqual(summary.total_cost_usd, _AGGREGATES_BREAKDOWNS_TOTAL_COST_USD)
        self.assertEqual(summary.total_input_tokens, 100)
        self.assertEqual(summary.total_output_tokens, _AGGREGATES_BREAKDOWNS_TOTAL_OUTPUT_TOKENS)
        # Insertion order must match `c DESC, label ASC` so the
        # dashboard's iteration order does not depend on which UNION
        # plan Postgres picked.
        self.assertEqual(
            list(summary.by_event.items()),
            [(_STAGE_ENTER, 30), (_AGENT_EXIT, 12)],
        )
        self.assertEqual(
            list(summary.by_stage.items()),
            [("implementing", 20), ("validating", 10)],
        )
        # And the whole result came from a single round-trip.
        self.assertEqual(len(conn.executed), 1)

    def test_window_and_repo_params_bound(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_summary(
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO_SHORT,
            connect=conn.as_connect,
        )
        # The single combined SQL applies the filter once in the CTE
        # and the totals / breakdown branches inherit it from `win`.
        # The Layer 4 cutover swapped the events-table scan for the
        # daily rollup, so the window predicate is now on `day`
        # (the rollup's UTC-bound aggregate key) and the parameters
        # carry the `.date()` projection of the input timestamps.
        self.assertEqual(len(conn.executed), 1)
        sql, query_params = conn.first_query
        self.assertIn(_WIN_CTE, sql)
        self.assertIn(_ROLLUP_SCAN, sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        bound_window = (_WINDOW_START.date(), _WINDOW_END.date(), _REPO_SHORT)
        self.assertEqual(query_params[:3], bound_window)

    def test_distinct_issues_counts_repo_issue_pairs(self) -> None:
        # GitHub issue numbers are only unique within a repo, so a
        # multi-repo window must count `(repo, issue)` pairs, not bare
        # `issue`. Otherwise `owner/a#1` and `owner/b#1` would collapse
        # into one and undercount activity. The fake here represents a
        # window that holds two distinct (repo, issue) pairs sharing
        # issue=1; the SQL must read `COUNT(DISTINCT (repo, issue))` so
        # the fake aggregate reflecting `2` round-trips into the
        # `distinct_issues` field.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _WIN_CTE: [
                (_KIND_TOTALS, None, 4, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary.distinct_issues, 2)
        sql, _ = conn.first_query
        self.assertIn("COUNT(DISTINCT (repo, issue))", sql)


class SummaryAgentRunsExtensionTest(unittest.TestCase):
    """The summary totals SQL emits `total_agent_runs` and
    `failed_agent_runs` (scoped to `event = 'agent_exit'` rows
    inside the same window) so the dashboard's success-rate panel
    reads off the same query as the rest of the overview."""

    def test_totals_carry_agent_run_columns(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # Full 13-column totals row: kind / label / events / issues
        # / repos / cost / input / output / total runs / failed runs
        # / cache_read / cache_write / timed_out. Layer 3's combined
        # SQL keeps every aggregate column on the totals row; Layer 4
        # swaps the events-table scan for the daily rollup so the
        # aggregates are recovered from the rollup's pre-derived
        # `failed_count` (`exit_code IS NOT NULL AND exit_code <> 0`)
        # narrowed to `event = 'agent_exit'`.
        conn.rows_for = {
            _WIN_CTE: [
                (_KIND_TOTALS, None, 50, 12, 3, 2.5, 100, 200, 15, 4, 0, 0, 0),
            ],
        }
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary.total_agent_runs, _AGENT_RUN_COLUMNS_TOTAL_AGENT_RUNS)
        self.assertEqual(summary.failed_agent_runs, 4)
        sql, _ = conn.first_query
        self.assertIn("total_agent_runs", sql)
        self.assertIn("failed_agent_runs", sql)
        # Failure subset constrains on `event = 'agent_exit'` so a
        # non-exit row that happened to carry a non-null exit code
        # never counts; the NULL-exit-code guard lives in the rollup
        # definition.
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn(_ROLLUP_SCAN, sql)

    def test_short_totals_tuple_round_trips(self) -> None:
        # A fixture whose totals row is shorter than the full
        # 13-column shape (e.g. a pre-extension fake) defaults the
        # missing trailing columns to zero rather than raising on
        # the unpack. Mirrors the previous "legacy 6-tuple" guard.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _WIN_CTE: [
                # kind / label / events / issues / repos / cost /
                # input / output -- no agent-run or cache columns.
                (_KIND_TOTALS, None, 4, 2, 2, 0, 0, 0),
            ],
        }
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary.total_agent_runs, 0)
        self.assertEqual(summary.failed_agent_runs, 0)
        self.assertEqual(summary.total_cache_read_tokens, 0)
        self.assertEqual(summary.total_cache_write_tokens, 0)
        self.assertEqual(summary.timed_out_agent_runs, 0)

    def test_totals_carry_cache_token_columns(self) -> None:
        # The cache columns feed the redesigned "Total tokens" KPI
        # and sparkline -- matching the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _WIN_CTE: [
                (_KIND_TOTALS, None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 0),
            ],
        }
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary.total_cache_read_tokens, _CACHE_TOKEN_COLUMNS_TOTAL_CACHE_READ_TOKEN)
        self.assertEqual(summary.total_cache_write_tokens, _CACHE_TOKEN_COLUMNS_TOTAL_CACHE_WRITE_TOKE)
        sql, _ = conn.first_query
        # The rollup carries cache-band tokens pre-summed per group
        # under the `total_cache_*` column names, so the reader sums
        # the rollup columns rather than the raw event columns.
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_totals_carry_timed_out_agent_runs(self) -> None:
        # Window-wide `timed_out` count so the reliability "Timeouts"
        # tile aggregates every timed-out run in the window -- not
        # just the latest N from `get_recent_agent_exits`. The SQL
        # filters on `timed_out = true` so NULL pre-flag rows never
        # count.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _WIN_CTE: [
                (_KIND_TOTALS, None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 7),
            ],
        }
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(summary.timed_out_agent_runs, 7)
        sql, _ = conn.first_query
        self.assertIn("timed_out", sql)
        self.assertIn("timed_out_agent_runs", sql)


class KpiPrevTest(unittest.TestCase):
    """Layer 3's `get_kpi_prev`: a single-query previous-window
    reader that returns only the cost / token / agent-run scalars the
    dashboard's KPI delta pills and cost-trend banner consume.
    Public return type is still `Summary` so existing call sites
    (`compute_insights`, the dashboard's `prev_summary` consumers)
    keep their shape; the unread fields stay at their dataclass
    defaults."""

    def test_returns_zero_summary_when_db_url_unset(self) -> None:
        analytics_read = _reload_read(db_url="")
        connected: list[str] = []
        summary = analytics_read.get_kpi_prev(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(summary, analytics_read.Summary())

    def test_returns_zero_summary_for_empty_window(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {}
        summary = analytics_read.get_kpi_prev(connect=conn.as_connect)
        self.assertEqual(summary, analytics_read.Summary())

    def test_scalars_round_trip(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 6-tuple: cost / input / output / cache_read / cache_write
        # / total_agent_runs -- exactly what the dashboard reads off
        # `prev_summary` for KPI deltas and the cost-trend banner.
        conn.rows_for = {
            _SCALARS_CTE: [(2.5, 1000, 500, 200, 100, 7)],
        }
        summary = analytics_read.get_kpi_prev(connect=conn.as_connect)
        # Contract one: each scalar the KPI strip reads off `prev_summary`
        # round-trips from its own row column -- asserted field-by-field
        # so a column-order regression is pinned to the exact scalar.
        for field, expected in (
            ("total_cost_usd", 2.5),
            ("total_input_tokens", 1000),
            ("total_output_tokens", 500),
            ("total_cache_read_tokens", 200),
            ("total_cache_write_tokens", 100),
            ("total_agent_runs", 7),
        ):
            self.assertEqual(getattr(summary, field), expected, field)
        # Contract two: the trimmed reader leaves every unread field at
        # its dataclass default so consumers see zero, not stale values.
        for field, expected in (
            ("total_events", 0),
            ("distinct_issues", 0),
            ("distinct_repos", 0),
            ("failed_agent_runs", 0),
            ("timed_out_agent_runs", 0),
            ("by_event", {}),
            ("by_stage", {}),
        ):
            self.assertEqual(getattr(summary, field), expected, field)

    def test_window_and_filter_params_bound(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(
            start=_PREV_START,
            end=_WINDOW_START,
            repo=_REPO_SHORT,
            events=[_AGENT_EXIT],
            stages=["implementing"],
            connect=conn.as_connect,
        )
        # One round-trip; the rollup window predicate replaces the
        # base-table `ts >= / ts <` shape with `day >= / day <`,
        # but the previous-window read still narrows alongside the
        # current-window summary.
        self.assertEqual(len(conn.executed), 1)
        sql, query_params = conn.first_query
        self.assertIn(_ROLLUP_SCAN, sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn("event IN (%s)", sql)
        self.assertIn("stage IN (%s)", sql)
        bound_window = (_PREV_START.date(), _WINDOW_START.date(), _REPO_SHORT)
        self.assertEqual(query_params[:3], bound_window)

    def test_empty_events_emits_false_predicate(self) -> None:
        # Mirrors `get_summary`'s cleared-multiselect semantics: an
        # empty events list means "no rows match" rather than "no
        # filter". The SQL carries a tautologically-false predicate
        # so the previous-window KPI strip drops to zero alongside
        # the current-window read.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(events=[], connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("FALSE", sql)

    def test_skips_breakdown_and_distinct_counts(self) -> None:
        # The trimmed shape is the whole point: the SQL must NOT
        # carry the `GROUP BY` follow-ups or the
        # `COUNT(DISTINCT ...)`s that `get_summary` emits, otherwise
        # the previous-window read still pays the same cost it did
        # before Layer 3.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertNotIn("GROUP BY", sql)
        self.assertNotIn("COUNT(DISTINCT", sql)

    def test_short_row_round_trips(self) -> None:
        # A fake that pre-dates the agent-run column still returns a
        # valid `Summary` with the missing trailing column defaulted
        # to zero -- mirrors the `get_summary` legacy-tuple guard.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _SCALARS_CTE: [(1.0, 100, 200, 50, 25)],
        }
        summary = analytics_read.get_kpi_prev(connect=conn.as_connect)
        self.assertEqual(summary.total_cost_usd, 1.0)
        self.assertEqual(summary.total_agent_runs, 0)


class TimeSeriesTest(unittest.TestCase):
    def test_unset_db_url_returns_empty_list(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_time_series(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_groups_by_day_and_event(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # Reads the daily rollup -- the rollup's `day` column is
        # the GROUP BY key, so the SQL no longer needs a
        # `date_trunc('day', ts)` expression.
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_TS_DAY, _STAGE_ENTER, 5),
                (_TS_DAY, _AGENT_EXIT, 2),
                (_TS_NEXT_DAY, _STAGE_ENTER, 7),
            ],
        }
        points = analytics_read.get_time_series(connect=conn.as_connect)
        self.assertEqual(
            points,
            [
                analytics_read.TimeSeriesPoint(_TS_DAY, _STAGE_ENTER, 5),
                analytics_read.TimeSeriesPoint(_TS_DAY, _AGENT_EXIT, 2),
                analytics_read.TimeSeriesPoint(_TS_NEXT_DAY, _STAGE_ENTER, 7),
            ],
        )

    def test_datetime_day_normalised_to_date(self) -> None:
        # Some drivers return the `day` column as a timestamp even
        # when the underlying type is `date`; the read model
        # normalises so the dashboard sees `date`.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (datetime(_YEAR, 5, _DAY_NORMALISED_DATE_DAY, 0, 0, tzinfo=timezone.utc), "x", 1),
            ],
        }
        points = analytics_read.get_time_series(connect=conn.as_connect)
        self.assertEqual(points[0].day, _TS_DAY)
        self.assertEqual(points[0].count, 1)


class TimeSeriesAggregatesTest(unittest.TestCase):
    """Reshaped `get_time_series` carries per-(day, event) cost /
    token aggregates so the spend-over-time and tokens-over-time
    charts can pivot the same query."""

    def test_aggregates_round_trip(self) -> None:
        # 8-tuple: day / event / count / cost / input / output /
        # cache_read / cache_write. The cache columns feed the
        # redesigned hero chart's Cache band. After Layer 4 the
        # reader sums the rollup's pre-derived `total_*` columns
        # instead of the raw event-table columns.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_TS_DAY, _AGENT_EXIT, 3, 0.42, 1000, 500, 200, 100),
            ],
        }
        points = analytics_read.get_time_series(connect=conn.as_connect)
        self.assertEqual(len(points), 1)
        point = points[0]
        assert_row_fields(
            self,
            point,
            {
                "count": 3,
                "cost_usd": _AGGREGATES_ROUND_TRIP_COST_USD,
                "input_tokens": 1000,
                "output_tokens": _AGGREGATES_ROUND_TRIP_OUTPUT_TOKENS,
                "cache_read_tokens": _AGGREGATES_ROUND_TRIP_CACHE_READ_TOKENS,
                "cache_write_tokens": 100,
            },
        )
        sql, _ = conn.first_query
        # Every per-day aggregate is summed from the rollup's
        # pre-derived `total_*` columns, not the raw event columns.
        assert_sql_fragments(
            self,
            sql,
            tuple(
                "SUM({0})".format(rollup_column)
                for rollup_column in (
                    "total_cost_usd",
                    "total_input_tokens",
                    "total_output_tokens",
                    "total_cache_read_tokens",
                    "total_cache_write_tokens",
                )
            ),
        )

    def test_legacy_six_tuple_defaults_cache_to_zero(self) -> None:
        # Older fixtures still emit 6-tuple `(day, event, count,
        # cost, in, out)` rows; the reader defaults the cache fields
        # to zero so unrelated tests round-trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_TS_DAY, _AGENT_EXIT, 3, 0.42, 1000, 500),
            ],
        }
        point = analytics_read.get_time_series(connect=conn.as_connect)[0]
        self.assertEqual(point.cache_read_tokens, 0)
        self.assertEqual(point.cache_write_tokens, 0)
