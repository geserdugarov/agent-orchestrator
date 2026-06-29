# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


class SummaryTest(unittest.TestCase):
    """Date-bounded aggregate counts plus per-event / per-stage
    breakdowns. Empty results give a zero-valued Summary, not None."""

    def test_returns_zero_summary_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_summary(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.Summary())

    def test_empty_rows_yield_zero_summary(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # No rows from the unioned SELECT (a fake that emits nothing).
        conn.rows_for = {}
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result, analytics_read.Summary())

    def test_aggregates_and_breakdowns(self) -> None:
        # Layer 3 collapses totals + by_event + by_stage into one
        # UNION-ALL'd query keyed by a `kind` discriminator. Each
        # row carries the 13-column shape; the by_event / by_stage
        # rows only populate `kind`, `label`, and `count_val`, with
        # trailing NULLs that the reader ignores. The fixture emits
        # the breakdown pairs in arbitrary order so the in-Python
        # `COUNT DESC, label ASC` sort that preserves the previous
        # SQL ordering is exercised.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 42, 10, 2, 1.234, 100, 200, 0, 0, 0, 0, 0),
                ("e", "agent_exit", 12, None, None, None, None, None,
                 None, None, None, None, None),
                ("e", "stage_enter", 30, None, None, None, None, None,
                 None, None, None, None, None),
                ("s", "validating", 10, None, None, None, None, None,
                 None, None, None, None, None),
                ("s", "implementing", 20, None, None, None, None, None,
                 None, None, None, None, None),
            ],
        }
        result = analytics_read.get_summary(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 28, tzinfo=timezone.utc),
            repo="owner/repo",
            connect=_connector(conn),
        )
        self.assertEqual(result.total_events, 42)
        self.assertEqual(result.distinct_issues, 10)
        self.assertEqual(result.distinct_repos, 2)
        self.assertEqual(result.total_cost_usd, 1.234)
        self.assertEqual(result.total_input_tokens, 100)
        self.assertEqual(result.total_output_tokens, 200)
        # Insertion order must match `c DESC, label ASC` so the
        # dashboard's iteration order does not depend on which UNION
        # plan Postgres picked.
        self.assertEqual(
            list(result.by_event.items()),
            [("stage_enter", 30), ("agent_exit", 12)],
        )
        self.assertEqual(
            list(result.by_stage.items()),
            [("implementing", 20), ("validating", 10)],
        )
        # And the whole result came from a single round-trip.
        self.assertEqual(len(conn.executed), 1)

    def test_window_and_repo_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_summary(
            start=start, end=end, repo="owner/r",
            connect=_connector(conn),
        )
        # The single combined SQL applies the filter once in the CTE
        # and the totals / breakdown branches inherit it from `win`.
        # The Layer 4 cutover swapped the events-table scan for the
        # daily rollup, so the window predicate is now on `day`
        # (the rollup's UTC-bound aggregate key) and the parameters
        # carry the `.date()` projection of the input timestamps.
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("WITH win AS", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertEqual(params[:3], (start.date(), end.date(), "owner/r"))

    def test_distinct_issues_counts_repo_issue_pairs(self) -> None:
        # GitHub issue numbers are only unique within a repo, so a
        # multi-repo window must count `(repo, issue)` pairs, not bare
        # `issue`. Otherwise `owner/a#1` and `owner/b#1` would collapse
        # into one and undercount activity. The fake here represents a
        # window that holds two distinct (repo, issue) pairs sharing
        # issue=1; the SQL must read `COUNT(DISTINCT (repo, issue))` so
        # the fake aggregate reflecting `2` round-trips into the
        # `distinct_issues` field.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 4, 2, 2, 0.0, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.distinct_issues, 2)
        sql, _ = conn.executed[0]
        self.assertIn("COUNT(DISTINCT (repo, issue))", sql)


class SummaryAgentRunsExtensionTest(unittest.TestCase):
    """The summary totals SQL emits `total_agent_runs` and
    `failed_agent_runs` (scoped to `event = 'agent_exit'` rows
    inside the same window) so the dashboard's success-rate panel
    reads off the same query as the rest of the overview."""

    def test_totals_carry_agent_run_columns(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
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
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 15)
        self.assertEqual(result.failed_agent_runs, 4)
        sql, _ = conn.executed[0]
        self.assertIn("total_agent_runs", sql)
        self.assertIn("failed_agent_runs", sql)
        # Failure subset constrains on `event = 'agent_exit'` so a
        # non-exit row that happened to carry a non-null exit code
        # never counts; the NULL-exit-code guard lives in the rollup
        # definition.
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)

    def test_short_totals_tuple_round_trips(self) -> None:
        # A fixture whose totals row is shorter than the full
        # 13-column shape (e.g. a pre-extension fake) defaults the
        # missing trailing columns to zero rather than raising on
        # the unpack. Mirrors the previous "legacy 6-tuple" guard.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                # kind / label / events / issues / repos / cost /
                # input / output -- no agent-run or cache columns.
                ("t", None, 4, 2, 2, 0.0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 0)
        self.assertEqual(result.failed_agent_runs, 0)
        self.assertEqual(result.total_cache_read_tokens, 0)
        self.assertEqual(result.total_cache_write_tokens, 0)
        self.assertEqual(result.timed_out_agent_runs, 0)

    def test_totals_carry_cache_token_columns(self) -> None:
        # The cache columns feed the redesigned "Total tokens" KPI
        # and sparkline -- matching the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_cache_read_tokens, 1200)
        self.assertEqual(result.total_cache_write_tokens, 800)
        sql, _ = conn.executed[0]
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
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 7),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.timed_out_agent_runs, 7)
        sql, _ = conn.executed[0]
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
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected: list[str] = []
        result = analytics_read.get_kpi_prev(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.Summary())

    def test_returns_zero_summary_for_empty_window(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {}
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result, analytics_read.Summary())

    def test_scalars_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 6-tuple: cost / input / output / cache_read / cache_write
        # / total_agent_runs -- exactly what the dashboard reads off
        # `prev_summary` for KPI deltas and the cost-trend banner.
        conn.rows_for = {
            "AS total_cost_usd": [(2.5, 1000, 500, 200, 100, 7)],
        }
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result.total_cost_usd, 2.5)
        self.assertEqual(result.total_input_tokens, 1000)
        self.assertEqual(result.total_output_tokens, 500)
        self.assertEqual(result.total_cache_read_tokens, 200)
        self.assertEqual(result.total_cache_write_tokens, 100)
        self.assertEqual(result.total_agent_runs, 7)
        # The trimmed reader leaves the unread fields at their
        # dataclass defaults so existing `prev_summary` consumers
        # see zero rather than stale values.
        self.assertEqual(result.total_events, 0)
        self.assertEqual(result.distinct_issues, 0)
        self.assertEqual(result.distinct_repos, 0)
        self.assertEqual(result.failed_agent_runs, 0)
        self.assertEqual(result.timed_out_agent_runs, 0)
        self.assertEqual(result.by_event, {})
        self.assertEqual(result.by_stage, {})

    def test_window_and_filter_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 1, tzinfo=timezone.utc)
        analytics_read.get_kpi_prev(
            start=start, end=end, repo="owner/r",
            events=["agent_exit"], stages=["implementing"],
            connect=_connector(conn),
        )
        # One round-trip; the rollup window predicate replaces the
        # base-table `ts >= / ts <` shape with `day >= / day <`,
        # but the previous-window read still narrows alongside the
        # current-window summary.
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn("event IN (%s)", sql)
        self.assertIn("stage IN (%s)", sql)
        self.assertEqual(params[:3], (start.date(), end.date(), "owner/r"))

    def test_empty_events_emits_false_predicate(self) -> None:
        # Mirrors `get_summary`'s cleared-multiselect semantics: an
        # empty events list means "no rows match" rather than "no
        # filter". The SQL carries a tautologically-false predicate
        # so the previous-window KPI strip drops to zero alongside
        # the current-window read.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(events=[], connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FALSE", sql)

    def test_does_not_emit_breakdown_or_distinct_counts(self) -> None:
        # The trimmed shape is the whole point: the SQL must NOT
        # carry the `GROUP BY` follow-ups or the
        # `COUNT(DISTINCT ...)`s that `get_summary` emits, otherwise
        # the previous-window read still pays the same cost it did
        # before Layer 3.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertNotIn("GROUP BY", sql)
        self.assertNotIn("COUNT(DISTINCT", sql)

    def test_short_row_round_trips(self) -> None:
        # A fake that pre-dates the agent-run column still returns a
        # valid `Summary` with the missing trailing column defaulted
        # to zero -- mirrors the `get_summary` legacy-tuple guard.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "AS total_cost_usd": [(1.0, 100, 200, 50, 25)],
        }
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result.total_cost_usd, 1.0)
        self.assertEqual(result.total_agent_runs, 0)


class TimeSeriesTest(unittest.TestCase):

    def test_unset_db_url_returns_empty_list(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_time_series(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_groups_by_day_and_event(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # Reads the daily rollup -- the rollup's `day` column is
        # the GROUP BY key, so the SQL no longer needs a
        # `date_trunc('day', ts)` expression.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (date(2026, 5, 25), "stage_enter", 5),
                (date(2026, 5, 25), "agent_exit", 2),
                (date(2026, 5, 26), "stage_enter", 7),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(
            points,
            [
                analytics_read.TimeSeriesPoint(date(2026, 5, 25), "stage_enter", 5),
                analytics_read.TimeSeriesPoint(date(2026, 5, 25), "agent_exit", 2),
                analytics_read.TimeSeriesPoint(date(2026, 5, 26), "stage_enter", 7),
            ],
        )

    def test_datetime_day_normalised_to_date(self) -> None:
        # Some drivers return the `day` column as a timestamp even
        # when the underlying type is `date`; the read model
        # normalises so the dashboard sees `date`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc), "x", 1),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(points[0].day, date(2026, 5, 25))
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
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (
                    date(2026, 5, 25), "agent_exit",
                    3, 0.42, 1000, 500, 200, 100,
                ),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(len(points), 1)
        p = points[0]
        self.assertEqual(p.count, 3)
        self.assertEqual(p.cost_usd, 0.42)
        self.assertEqual(p.input_tokens, 1000)
        self.assertEqual(p.output_tokens, 500)
        self.assertEqual(p.cache_read_tokens, 200)
        self.assertEqual(p.cache_write_tokens, 100)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(total_cost_usd)", sql)
        self.assertIn("SUM(total_input_tokens)", sql)
        self.assertIn("SUM(total_output_tokens)", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_legacy_six_tuple_rows_default_cache_to_zero(self) -> None:
        # Older fixtures still emit 6-tuple `(day, event, count,
        # cost, in, out)` rows; the reader defaults the cache fields
        # to zero so unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (date(2026, 5, 25), "agent_exit", 3, 0.42, 1000, 500),
            ],
        }
        p = analytics_read.get_time_series(connect=_connector(conn))[0]
        self.assertEqual(p.cache_read_tokens, 0)
        self.assertEqual(p.cache_write_tokens, 0)
