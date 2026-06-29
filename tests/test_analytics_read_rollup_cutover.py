# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Callable

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


class RollupReadCutoverTest(unittest.TestCase):
    """Layer 4 cutover: every reader the issue calls out reads from
    `analytics_daily_rollup` instead of `analytics_events` /
    `analytics_agent_runs`. The previous reader-shape tests above
    already cover the column wiring; this class concentrates on the
    semantic invariants the cutover has to preserve.

    The rollup is keyed on `(day, repo, issue, event, stage,
    backend, cost_source)` with `day = (ts AT TIME ZONE 'UTC')::date`
    and aggregates `event_count`, `failed_count`, `timed_out_count`,
    `total_cost_usd`, the token sums, and `duration_s_sum` /
    `duration_s_count`. The dashboard passes midnight-aligned UTC
    `[start, end)` windows so the rollup is semantically equivalent
    to a `ts`-scoped scan; these tests pin that down by checking
    parameter bindings, filter shapes, and column accounting.
    """

    def _rollup_readers(self, analytics_read) -> list[Callable[..., Any]]:
        # The seven cutover readers in the order the issue lists
        # them. `get_summary` and `get_kpi_prev` carry the same
        # `_build_rollup_window_where` shape; `get_throughput_breakdown`
        # builds its WHERE inline but still uses `day` rather than `ts`.
        return [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
            analytics_read.get_backend_efficiency,
            analytics_read.get_throughput_breakdown,
        ]

    def test_every_cutover_reader_queries_the_rollup(self) -> None:
        # No cutover reader may regress to `analytics_events` or
        # `analytics_agent_runs` -- the whole point of the migration
        # is the rollup-backed scan. A single check against every
        # reader in one place keeps a future reader rewrite from
        # silently dropping the rollup target.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(connect=_connector(conn))
            self.assertEqual(
                len(conn.executed), 1,
                f"{reader.__name__} must issue one round-trip",
            )
            sql = conn.executed[0][0]
            self.assertIn(
                "FROM analytics_daily_rollup", sql,
                f"{reader.__name__} must read from the rollup, "
                f"got SQL: {sql}",
            )
            self.assertNotIn(
                "FROM analytics_events", sql,
                f"{reader.__name__} must not regress to "
                f"analytics_events",
            )
            self.assertNotIn(
                "FROM analytics_agent_runs", sql,
                f"{reader.__name__} must not regress to "
                f"analytics_agent_runs",
            )

    def test_window_predicate_uses_day_with_date_params(self) -> None:
        # The dashboard's `to_window` produces midnight-aligned UTC
        # datetimes; the rollup is keyed by `day` (a UTC date), so
        # the helper must project `start`/`end` to `.date()` before
        # binding so the query plan stays a day-range scan rather
        # than a cast at execute time.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(start=start, end=end, connect=_connector(conn))
            sql, params = conn.executed[0]
            self.assertIn(
                "day >= %s", sql,
                f"{reader.__name__} must use day-keyed lower bound",
            )
            self.assertIn(
                "day < %s", sql,
                f"{reader.__name__} must use day-keyed upper bound",
            )
            self.assertIn(start.date(), params)
            self.assertIn(end.date(), params)

    def test_issue_filter_narrows_every_reader(self) -> None:
        # The rollup key carries `issue`, so the `issue = %s`
        # predicate still narrows the scan. The dashboard refuses
        # to apply this filter unless `repo` is also set (issue
        # numbers are only unique within a repo); the helper itself
        # does not enforce that, so we mirror the dashboard's call
        # shape (`repo=..., issue=...`).
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(repo="owner/r", issue=42, connect=_connector(conn))
            sql, params = conn.executed[0]
            self.assertIn(
                "issue = %s", sql,
                f"{reader.__name__} must thread the issue filter "
                f"into the rollup scan",
            )
            self.assertIn(42, params)

    def test_event_filter_clears_to_empty_predicate(self) -> None:
        # Cleared-multiselect contract: an empty list means "no
        # rows match" rather than "no filter". `get_backend_efficiency`
        # is excluded because it short-circuits via
        # `_agent_event_excluded` before building SQL (cleared events
        # selection = no agent_exit selected = return []); the other
        # cutover readers that take an `events=` param emit the
        # tautologically-false predicate. `get_throughput_breakdown`
        # has its own short-circuit on the implicit `stage_enter`
        # constraint -- so it also returns [] without SQL when
        # events is cleared.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
        ]
        for reader in emits_predicate:
            conn = _FakeConnection()
            reader(events=[], connect=_connector(conn))
            sql = conn.executed[0][0]
            self.assertIn(
                "FALSE", sql,
                f"{reader.__name__} must emit a tautologically-false "
                f"predicate when the events multiselect is cleared",
            )

    def test_stage_filter_clears_to_empty_predicate(self) -> None:
        # Mirrors the events-filter contract: an empty stages list
        # is the dashboard's cleared-multiselect signal. Same set
        # of readers as `test_event_filter_clears_to_empty_predicate`
        # because `get_backend_efficiency` does not short-circuit on
        # stages, but the FALSE predicate is what makes its result
        # drop to zero alongside the rest.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
            analytics_read.get_backend_efficiency,
        ]
        for reader in emits_predicate:
            conn = _FakeConnection()
            reader(stages=[], connect=_connector(conn))
            sql = conn.executed[0][0]
            self.assertIn(
                "FALSE", sql,
                f"{reader.__name__} must emit a tautologically-false "
                f"predicate when the stages multiselect is cleared",
            )

    def test_summary_recovers_token_and_timeout_aggregates(self) -> None:
        # The dashboard's KPI strip reads `total_input_tokens`,
        # `total_output_tokens`, `total_cache_read_tokens`,
        # `total_cache_write_tokens`, `total_cost_usd`, and
        # `timed_out_agent_runs` off `get_summary`. The rollup
        # carries the per-bucket sums under `total_*` columns and
        # `timed_out_count` is pre-scoped to `event = 'agent_exit'
        # AND timed_out = TRUE`, so a plain SUM recovers each
        # KPI's value verbatim. This test pins the column
        # accounting end-to-end so a future rollup column rename
        # cannot silently zero out a KPI.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 13-column totals row matching the cutover SQL's projection
        # order: kind / label / events / issues / repos / cost /
        # input / output / total_runs / failed_runs / cache_read /
        # cache_write / timed_out.
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 200, 24, 3,
                 4.5, 12_000, 8_000, 35, 6, 3_000, 1_500, 11),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_input_tokens, 12_000)
        self.assertEqual(result.total_output_tokens, 8_000)
        self.assertEqual(result.total_cache_read_tokens, 3_000)
        self.assertEqual(result.total_cache_write_tokens, 1_500)
        self.assertEqual(result.total_cost_usd, 4.5)
        self.assertEqual(result.total_agent_runs, 35)
        self.assertEqual(result.failed_agent_runs, 6)
        # The reliability "Timeouts" tile reads off this field --
        # the rollup's `timed_out_count` is already
        # `event = 'agent_exit' AND timed_out = TRUE`-scoped, so a
        # plain SUM is correct without an extra CASE in the reader.
        self.assertEqual(result.timed_out_agent_runs, 11)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(timed_out_count)", sql)
        self.assertIn("SUM(total_input_tokens)", sql)
        self.assertIn("SUM(total_output_tokens)", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_stage_breakdown_recovers_weighted_duration_average(self) -> None:
        # `AVG(duration_s)` cannot be reconstructed from per-day
        # rollup averages without double-averaging (averaging
        # averages across days does not preserve the row-weighted
        # mean), so the rollup carries `duration_s_sum` and
        # `duration_s_count` separately and the reader recovers
        # `AVG` as `SUM(sum) / SUM(count)`. The fake's pre-computed
        # `avg_dur` here mirrors what the SQL division produces;
        # the test pins the SQL shape so a future regression to a
        # naive `AVG(duration_s)` would fail.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # (stage, count, avg_dur, cost, input_tok, output_tok, runs)
        # Two stages: implementing (sum=125, count=10 -> 12.5),
        # validating (no row carried a non-null duration -> NULL).
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 10, 12.5, 0.50, 0, 0, 10),
                ("validating", 3, None, 0.05, 0, 0, 3),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[0].avg_duration_s, 12.5)
        # NULL preserved when no row in the window carried a
        # duration -- the dashboard hides the column rather than
        # showing a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)
        # Regression guard: the cutover MUST NOT regress to a plain
        # `AVG(duration_s)` over the rollup -- the rollup has no
        # such column, but more importantly averaging averages
        # across days would silently produce wrong numbers.
        self.assertNotIn("AVG(duration_s)", sql)

    def test_backend_efficiency_pins_event_filter_in_sql(self) -> None:
        # The previous `analytics_agent_runs` view filtered to
        # `event = 'agent_exit'` internally. The rollup has an
        # `event` column, so the reader pins the filter in the
        # WHERE clause directly -- this is how the cutover
        # preserves the prior view's row scope.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_backend_efficiency(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("event = 'agent_exit'", sql)
        # And the agent-event short-circuit still wins over the
        # pinned filter when the operator excludes `agent_exit`
        # from the multiselect: no SQL emitted.
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_throughput_breakdown_uses_day_window(self) -> None:
        # `get_throughput_breakdown` builds its WHERE clause inline
        # (it carries a hardcoded `event = 'stage_enter'` predicate),
        # so the Layer 4 cutover has to migrate that branch too.
        # The window must bind `.date()` values against the rollup
        # `day` column rather than the previous `ts >= / ts <`
        # pair against the events table.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_throughput_breakdown(
            start=start, end=end, repo="owner/r", issue=42,
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("event = %s", sql)
        self.assertIn("stage_enter", params)
        # `.date()` binding so the planner sees a date-range scan
        # against the `(day, repo)` supporting index.
        self.assertIn(start.date(), params)
        self.assertIn(end.date(), params)
        self.assertIn("owner/r", params)
        self.assertIn(42, params)
