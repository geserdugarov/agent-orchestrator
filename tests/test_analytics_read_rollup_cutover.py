# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Callable

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The rollup-backed scan and the two regressed base tables every
# cutover assertion checks against, plus the filter literals the
# readers thread through.
_ROLLUP_SCAN = "FROM analytics_daily_rollup"
_EVENTS_SCAN = "FROM analytics_events"
_AGENT_RUNS_SCAN = "FROM analytics_agent_runs"
_STAGE_ENTER = "stage_enter"
_REPO_SHORT = "owner/r"

# Shared midnight-aligned UTC window and issue filter. The rollup
# binds the `.date()` projection of the window bounds, so the day
# components are the assertion surface rather than fixture noise.
_YEAR = 2026
_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 5, 28, tzinfo=timezone.utc)
_ISSUE = 42


def _rollup_readers(analytics_read) -> list[Callable[..., Any]]:
    # The seven cutover readers in the order the issue lists them.
    # `get_summary` and `get_kpi_prev` carry the same
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

    def test_every_cutover_reader_queries_the_rollup(self) -> None:
        # No cutover reader may regress to `analytics_events` or
        # `analytics_agent_runs` -- the whole point of the migration
        # is the rollup-backed scan. A single check against every
        # reader in one place keeps a future reader rewrite from
        # silently dropping the rollup target.
        analytics_read = _reload_read()
        for reader in _rollup_readers(analytics_read):
            with self.subTest(reader=reader.__name__):
                conn = _FakeConnection()
                reader(connect=conn.as_connect)
                self.assertEqual(len(conn.executed), 1)
                sql, _ = conn.first_query
                self.assertIn(_ROLLUP_SCAN, sql)
                self.assertNotIn(_EVENTS_SCAN, sql)
                self.assertNotIn(_AGENT_RUNS_SCAN, sql)

    def test_window_uses_day_and_date_params(self) -> None:
        # The dashboard's `to_window` produces midnight-aligned UTC
        # datetimes; the rollup is keyed by `day` (a UTC date), so
        # the helper must project `start`/`end` to `.date()` before
        # binding so the query plan stays a day-range scan rather
        # than a cast at execute time.
        analytics_read = _reload_read()
        for reader in _rollup_readers(analytics_read):
            with self.subTest(reader=reader.__name__):
                conn = _FakeConnection()
                reader(
                    start=_WINDOW_START, end=_WINDOW_END,
                    connect=conn.as_connect,
                )
                sql, query_params = conn.first_query
                self.assertIn("day >= %s", sql)
                self.assertIn("day < %s", sql)
                self.assertIn(_WINDOW_START.date(), query_params)
                self.assertIn(_WINDOW_END.date(), query_params)

    def test_issue_filter_narrows_every_reader(self) -> None:
        # The rollup key carries `issue`, so the `issue = %s`
        # predicate still narrows the scan. The dashboard refuses
        # to apply this filter unless `repo` is also set (issue
        # numbers are only unique within a repo); the helper itself
        # does not enforce that, so we mirror the dashboard's call
        # shape (`repo=..., issue=...`).
        analytics_read = _reload_read()
        for reader in _rollup_readers(analytics_read):
            with self.subTest(reader=reader.__name__):
                conn = _FakeConnection()
                reader(repo=_REPO_SHORT, issue=_ISSUE, connect=conn.as_connect)
                sql, query_params = conn.first_query
                self.assertIn("issue = %s", sql)
                self.assertIn(_ISSUE, query_params)

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
        analytics_read = _reload_read()
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
        ]
        for reader in emits_predicate:
            with self.subTest(reader=reader.__name__):
                conn = _FakeConnection()
                reader(events=[], connect=conn.as_connect)
                sql, _ = conn.first_query
                self.assertIn("FALSE", sql)

    def test_stage_filter_clears_to_empty_predicate(self) -> None:
        # Mirrors the events-filter contract: an empty stages list
        # is the dashboard's cleared-multiselect signal. Same set
        # of readers as `test_event_filter_clears_to_empty_predicate`
        # because `get_backend_efficiency` does not short-circuit on
        # stages, but the FALSE predicate is what makes its result
        # drop to zero alongside the rest.
        analytics_read = _reload_read()
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
            analytics_read.get_backend_efficiency,
        ]
        for reader in emits_predicate:
            with self.subTest(reader=reader.__name__):
                conn = _FakeConnection()
                reader(stages=[], connect=conn.as_connect)
                sql, _ = conn.first_query
                self.assertIn("FALSE", sql)


class RollupReadColumnAccountingTest(unittest.TestCase):
    """Per-reader column accounting for the Layer 4 cutover: each reader
    recovers its values verbatim from the rollup's pre-derived `total_*`
    / `duration_s_*` / `event` columns, so a future rollup column rename
    cannot silently zero out a reader's output."""

    def test_summary_recovers_token_timeout_totals(self) -> None:
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
        analytics_read = _reload_read()
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
        summary = analytics_read.get_summary(connect=conn.as_connect)
        self._assert_summary_totals(summary)
        sql, _ = conn.first_query
        for summed_column in (
            "timed_out_count",
            "total_input_tokens",
            "total_output_tokens",
            "total_cache_read_tokens",
            "total_cache_write_tokens",
        ):
            self.assertIn("SUM({0})".format(summed_column), sql)

    def test_recovers_weighted_stage_duration(self) -> None:
        # `AVG(duration_s)` cannot be reconstructed from per-day
        # rollup averages without double-averaging (averaging
        # averages across days does not preserve the row-weighted
        # mean), so the rollup carries `duration_s_sum` and
        # `duration_s_count` separately and the reader recovers
        # `AVG` as `SUM(sum) / SUM(count)`. The fake's pre-computed
        # `avg_dur` here mirrors what the SQL division produces;
        # the test pins the SQL shape so a future regression to a
        # naive `AVG(duration_s)` would fail.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # (stage, count, avg_dur, cost, input_tok, output_tok, runs)
        # Two stages: implementing (sum=125, count=10 -> 12.5),
        # validating (no row carried a non-null duration -> NULL).
        conn.rows_for = {
            _ROLLUP_SCAN: [
                ("implementing", 10, 12.5, 0.5, 0, 0, 10),
                ("validating", 3, None, 0.05, 0, 0, 3),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[0].avg_duration_s, 12.5)
        # NULL preserved when no row in the window carried a
        # duration -- the dashboard hides the column rather than
        # showing a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.first_query
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)
        # Regression guard: the cutover MUST NOT regress to a plain
        # `AVG(duration_s)` over the rollup -- the rollup has no
        # such column, but more importantly averaging averages
        # across days would silently produce wrong numbers.
        self.assertNotIn("AVG(duration_s)", sql)

    def test_backend_efficiency_pins_event_in_sql(self) -> None:
        # The previous `analytics_agent_runs` view filtered to
        # `event = 'agent_exit'` internally. The rollup has an
        # `event` column, so the reader pins the filter in the
        # WHERE clause directly -- this is how the cutover
        # preserves the prior view's row scope.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_backend_efficiency(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("event = 'agent_exit'", sql)
        # And the agent-event short-circuit still wins over the
        # pinned filter when the operator excludes `agent_exit`
        # from the multiselect: no SQL emitted.
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=[_STAGE_ENTER], connect=conn.as_connect,
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
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_throughput_breakdown(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO_SHORT,
            issue=_ISSUE, connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn(_ROLLUP_SCAN, sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("event = %s", sql)
        # `.date()` binding so the planner sees a date-range scan
        # against the `(day, repo)` supporting index.
        for bound in (
            _STAGE_ENTER,
            _WINDOW_START.date(),
            _WINDOW_END.date(),
            _REPO_SHORT,
            _ISSUE,
        ):
            self.assertIn(bound, query_params)

    def _assert_summary_totals(self, summary) -> None:
        # Each KPI the rollup recovers via a plain SUM round-trips from
        # its own column; asserting field-by-field keeps a column-rename
        # regression pinned to the exact KPI it would silently zero out.
        # `timed_out_agent_runs` is included because its rollup source
        # column is already `event = 'agent_exit' AND timed_out = TRUE`-
        # scoped, so a plain SUM recovers it verbatim.
        for field, expected in (
            ("total_events", 200),
            ("distinct_issues", 24),
            ("distinct_repos", 3),
            ("total_cost_usd", 4.5),
            ("total_input_tokens", 12_000),
            ("total_output_tokens", 8_000),
            ("total_agent_runs", 35),
            ("failed_agent_runs", 6),
            ("total_cache_read_tokens", 3_000),
            ("total_cache_write_tokens", 1_500),
            ("timed_out_agent_runs", 11),
        ):
            self.assertEqual(getattr(summary, field), expected, field)
