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


class BackendDailyTokensTest(unittest.TestCase):
    """`get_backend_daily_tokens` powers the redesigned dashboard's
    "By backend" hero toggle. It must read from the view, honor the
    agent-run event-filter short-circuit, and aggregate tokens across
    every agent run in the window (not a `LIMIT`-capped subset).
    """

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_backend_daily_tokens(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_reads_view_and_aggregates_per_day_per_backend(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                (date(2026, 5, 1), "claude", 12_000),
                (date(2026, 5, 1), "codex", 4_500),
                (date(2026, 5, 2), "claude", 8_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(
            connect=_connector(conn),
        )
        self.assertEqual(
            [(r.day, r.backend, r.total_tokens) for r in rows],
            [
                (date(2026, 5, 1), "claude", 12_000),
                (date(2026, 5, 1), "codex", 4_500),
                (date(2026, 5, 2), "claude", 8_000),
            ],
        )
        sql, _ = conn.executed[0]
        # Reads from the view -- so the agent-run filter contract
        # (no `event IN` clause) holds -- and groups by both day and
        # backend so the dashboard can build a per-day stack without
        # post-processing. Token total includes the cache band so
        # the backend stack matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("event IN", sql)
        self.assertIn("GROUP BY day, backend_label", sql)
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(col, sql)

    def test_null_backend_buckets_under_unknown(self) -> None:
        # `COALESCE(backend, 'unknown')` matches how
        # `get_backend_efficiency` surfaces NULL-backend rows.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                (date(2026, 5, 1), "unknown", 1_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].backend, "unknown")


class BackendEfficiencyTest(unittest.TestCase):
    """`get_backend_efficiency` aggregates the agent_runs view by
    backend and exposes failure / cost / token rollups."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_backend_efficiency(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 9-tuple: backend / runs / failed / avg_dur / cost /
        # input_tokens / output_tokens / cache_read / cache_write.
        # After Layer 4 the reader reads from the daily rollup
        # (with `event = 'agent_exit'` pinned to match the prior
        # view's filter); the fake fixture pre-computes the
        # weighted average so the reader's NULL handling still
        # rides through.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("claude", 20, 1, 35.0, 1.20, 5000, 4000, 1500, 800),
                ("codex", 10, 3, None, 0.40, 1000, 2000, 0, 0),
                ("unknown", 1, 0, None, 0.0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=_connector(conn))
        self.assertEqual([r.backend for r in rows], ["claude", "codex", "unknown"])
        self.assertEqual(rows[0].runs, 20)
        self.assertEqual(rows[0].failed, 1)
        self.assertEqual(rows[0].avg_duration_s, 35.0)
        self.assertEqual(rows[0].total_cost_usd, 1.20)
        # Cache columns feed the per-backend "cost / 1M tok" tile
        # alongside input + output so the denominator matches the
        # standalone mock's total-token accounting.
        self.assertEqual(rows[0].total_cache_read_tokens, 1500)
        self.assertEqual(rows[0].total_cache_write_tokens, 800)
        # NULL avg duration preserved so the dashboard can hide the
        # column rather than show a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        # The rollup carries an `event` column, so the cutover
        # query pins `event = 'agent_exit'` directly rather than
        # the view's implicit filter.
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn("COALESCE(backend, 'unknown')", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)
        # Weighted-duration recovery from the rollup, not
        # `AVG(duration_s)` over the raw events table.
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)

    def test_legacy_7tuple_fixture_defaults_cache_to_zero(self) -> None:
        # Older 7-tuple `(backend, runs, failed, avg_dur, cost, in,
        # out)` rows still round-trip with zero cache tokens so
        # unrelated tests keep working.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("claude", 5, 0, 10.0, 0.20, 1000, 500),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=_connector(conn))
        self.assertEqual(rows[0].total_cache_read_tokens, 0)
        self.assertEqual(rows[0].total_cache_write_tokens, 0)


class SkillTriggerRatesTest(unittest.TestCase):
    """`get_skill_trigger_rates` aggregates the base `analytics_events`
    table by `(agent_role, backend)` over the `extras` JSONB skill
    fields, honoring the same `agent_exit` event-filter contract as
    `get_backend_efficiency`."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_skill_trigger_rates(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_skill_trigger_rates(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        # No DB round-trip when the events filter excludes agent_exit.
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # (agent_role, backend, runs, skill_runs, total_triggers) --
        # mirrors the live-data table in the design doc.
        conn.rows_for = {
            "GROUP BY role_label, backend_label": [
                ("developer", "claude", 9, 3, 3),
                ("reviewer", "codex", 5, 0, 0),
                ("decomposer", "codex", 2, 0, 0),
            ],
        }
        rows = analytics_read.get_skill_trigger_rates(connect=_connector(conn))
        self.assertEqual(
            [(r.agent_role, r.backend) for r in rows],
            [
                ("developer", "claude"),
                ("reviewer", "codex"),
                ("decomposer", "codex"),
            ],
        )
        self.assertEqual(rows[0].runs, 9)
        self.assertEqual(rows[0].skill_runs, 3)
        self.assertEqual(rows[0].total_triggers, 3)
        self.assertAlmostEqual(rows[0].rate, 3 / 9)
        # The quiet reviewer reads as a real 0% trigger rate, not a
        # dropped category.
        self.assertEqual(rows[1].skill_runs, 0)
        self.assertEqual(rows[1].rate, 0.0)
        sql, _ = conn.executed[0]
        # Skill fields live in `extras` JSONB, which the rollup does
        # not carry, so the reader scans the base table and pins
        # agent_exit directly.
        self.assertIn("FROM analytics_events", sql)
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn("GROUP BY role_label, backend_label", sql)
        # Key-presence test (not the jsonb `?` operator) and the
        # trigger-count sum off `extras`.
        self.assertIn("extras -> 'skills_triggered' IS NOT NULL", sql)
        self.assertIn("skills_triggered_count", sql)

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # COALESCE maps NULL -> 'unknown' in SQL; the reader also
        # guards None defensively so a fake row without COALESCE still
        # round-trips.
        conn.rows_for = {
            "GROUP BY role_label, backend_label": [
                (None, None, 4, 0, 0),
            ],
        }
        rows = analytics_read.get_skill_trigger_rates(connect=_connector(conn))
        self.assertEqual(rows[0].agent_role, "unknown")
        self.assertEqual(rows[0].backend, "unknown")

    def test_rate_zero_runs_does_not_divide(self) -> None:
        # Defensive: a zero-run group (never emitted by the SQL) still
        # yields 0.0 rather than a ZeroDivisionError.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        row = analytics_read.SkillTriggerRateRow(
            agent_role="developer", backend="claude", runs=0,
        )
        self.assertEqual(row.rate, 0.0)

    def test_window_and_repo_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_skill_trigger_rates(
            start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 24, tzinfo=timezone.utc),
            repo="owner/repo",
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn(datetime(2026, 6, 1, tzinfo=timezone.utc), params)
        self.assertIn("owner/repo", params)


class RepoBreakdownTest(unittest.TestCase):
    """`get_repo_breakdown` reads the base table so the standard
    event/stage/date/repo/issue filter shape applies (no agent_runs
    short-circuit)."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_repo_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_per_repo_rows(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY repo": [
                ("owner/a", 5, 30, 4, 0.50),
                ("owner/b", 2, 10, 1, 0.10),
            ],
        }
        rows = analytics_read.get_repo_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].repo, "owner/a")
        self.assertEqual(rows[0].issues, 5)
        self.assertEqual(rows[0].events, 30)
        self.assertEqual(rows[0].agent_exits, 4)
        self.assertEqual(rows[0].total_cost_usd, 0.50)
        sql, _ = conn.executed[0]
        # GROUP BY repo with distinct issue count per row -- safe
        # because rollup rows are already scoped to one repo per
        # bucket and the rollup key carries `issue`.
        self.assertIn("COUNT(DISTINCT issue)", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)

    def test_event_filter_threaded(self) -> None:
        # `get_repo_breakdown` honors the standard event filter
        # because it reads the base table (which carries an `event`
        # column). Cleared multiselect -> FALSE predicate.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_repo_breakdown(
            events=[], connect=_connector(conn),
        )
        sql, _ = conn.executed[0]
        self.assertIn("FALSE", sql)


class CostCoverageTest(unittest.TestCase):
    """`get_cost_coverage` MUST keep `unknown-price` visible -- it is
    the maintenance signal for the pricing table in
    `orchestrator.usage`. Distinct from rows whose `cost_source` is
    NULL, which bucket under the generic `"unknown"`."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_cost_coverage(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_cost_coverage(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_unknown_price_preserved_verbatim(self) -> None:
        # The `unknown-price` slice surfaces with that exact label --
        # NEVER collapsed into "unknown" -- so the operator can see
        # which runs the parser could not price. The third tuple
        # column is the per-`cost_source` token rollup that feeds
        # the redesigned token-share coverage bar.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                ("reported", 20, 800_000),
                ("estimated", 5, 100_000),
                ("unknown-price", 3, 60_000),
                ("no-usage", 2, 20_000),
                ("unknown", 1, 5_000),
            ],
        }
        rows = analytics_read.get_cost_coverage(connect=_connector(conn))
        labels = [r.cost_source for r in rows]
        self.assertIn("unknown-price", labels)
        # Make sure we did not silently fold it into "unknown".
        self.assertEqual(
            sum(1 for r in rows if r.cost_source == "unknown-price"), 1,
        )
        self.assertEqual(
            sum(1 for r in rows if r.cost_source == "unknown"), 1,
        )
        # Per-source token volume rolls up alongside the run count.
        by_source = {r.cost_source: r for r in rows}
        self.assertEqual(by_source["reported"].total_tokens, 800_000)
        self.assertEqual(by_source["unknown-price"].total_tokens, 60_000)
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        # NULL cost_source rows bucket under "unknown" via COALESCE,
        # but the verbatim "unknown-price" string is untouched.
        self.assertIn("COALESCE(cost_source, 'unknown')", sql)
        # SQL totals input + output + cache_read + cache_write so the
        # token share matches the standalone mock's accounting.
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(col, sql)

    def test_legacy_two_tuple_rows_default_tokens_to_zero(self) -> None:
        # Older fixtures still emit 2-tuple `(cost_source, runs)`
        # rows; the reader defaults `total_tokens` to zero so
        # unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [("reported", 3)],
        }
        rows = analytics_read.get_cost_coverage(connect=_connector(conn))
        self.assertEqual(rows[0].total_tokens, 0)


class ReviewRoundBreakdownTest(unittest.TestCase):
    """`get_review_round_breakdown` reads from `analytics_agent_runs`
    so the agent-run filter contract (no `event` column in the view)
    is encoded as a Python-side short-circuit on `_agent_event_excluded`."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_review_round_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_query_against_view_and_buckets_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 12-tuple rows carry the role + cache split the new chart
        # consumes: (bucket, runs, failed, cost, dev_runs, rev_runs,
        # dev_cost, rev_cost, dev_cache, dev_no_cache, rev_cache,
        # rev_no_cache).
        conn.rows_for = {
            "analytics_agent_runs": [
                ("0", 12, 1, 40.0, 7, 5, 28.0, 12.0, 20.0, 8.0, 9.0, 3.0),
                ("1", 8, 2, 25.0, 4, 4, 10.0, 15.0, 7.0, 3.0, 11.0, 4.0),
                ("3-5", 4, 4, 18.0, 1, 3, 5.0, 13.0, 5.0, 0.0, 13.0, 0.0),
                ("unknown", 1, 0, 0.0, 1, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            ],
        }
        rows = analytics_read.get_review_round_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual([r.bucket for r in rows], ["0", "1", "3-5", "unknown"])
        self.assertEqual([r.runs for r in rows], [12, 8, 4, 1])
        self.assertEqual([r.failed for r in rows], [1, 2, 4, 0])
        # `total_cost_usd` powers the redesigned "Cost by review round"
        # chart in `orchestrator.dashboard_charts.cost_by_review_round`
        # and the "Rework share" KPI tile.
        self.assertEqual(
            [r.total_cost_usd for r in rows],
            [40.0, 25.0, 18.0, 0.0],
        )
        self.assertEqual([r.developer_runs for r in rows], [7, 4, 1, 1])
        self.assertEqual([r.reviewer_runs for r in rows], [5, 4, 3, 0])
        self.assertEqual(
            [r.developer_cost_usd for r in rows],
            [28.0, 10.0, 5.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_cost_usd for r in rows],
            [12.0, 15.0, 13.0, 0.0],
        )
        # Cache vs no-cache split per role -- the chart stacks these
        # so cache_cost + no_cache_cost must equal the role's total.
        self.assertEqual(
            [r.developer_cache_cost_usd for r in rows],
            [20.0, 7.0, 5.0, 0.0],
        )
        self.assertEqual(
            [r.developer_no_cache_cost_usd for r in rows],
            [8.0, 3.0, 0.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_cache_cost_usd for r in rows],
            [9.0, 11.0, 13.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_no_cache_cost_usd for r in rows],
            [3.0, 4.0, 0.0, 0.0],
        )
        sql, _ = conn.executed[0]
        # Reads from the view, not the base table, and the view has
        # no `event` column so no `event IN (...)` clause is emitted.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertIn("SUM(cost_usd)", sql)
        self.assertIn("agent_role IN ('developer', 'reviewer')", sql)
        self.assertIn("agent_role = 'developer'", sql)
        self.assertIn("agent_role = 'reviewer'", sql)
        self.assertIn("stage = 'implementing' THEN '0'", sql)
        self.assertNotIn("event IN", sql)
        # The cache / no-cache split is proportional: each run's cost
        # is weighted by the cache-token share of its billable token
        # volume. Codex `cached_tokens` is already a subset of
        # `input_tokens`, so it appears in the numerator only -- not
        # the denominator -- to avoid double-counting.
        self.assertIn("cached_tokens", sql)
        self.assertIn("cache_read_tokens", sql)
        self.assertIn("cache_write_tokens", sql)
        self.assertIn("developer_cache_cost_usd", sql)
        self.assertIn("developer_no_cache_cost_usd", sql)
        self.assertIn("reviewer_cache_cost_usd", sql)
        self.assertIn("reviewer_no_cache_cost_usd", sql)

    def test_legacy_three_tuple_rows_default_cost_to_zero(self) -> None:
        # Older fixtures still emit 3-tuple `(bucket, runs, failed)` rows
        # without the cost / role / cache rollups; the reader defaults
        # those values to zero so unrelated tests keep round-tripping.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"analytics_agent_runs": [("0", 3, 0)]}
        rows = analytics_read.get_review_round_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_no_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_no_cache_cost_usd, 0.0)

    def test_explicit_agent_exit_runs_query(self) -> None:
        # An events list that includes agent_exit must NOT short-circuit
        # -- the operator still wants to see the agent runs view.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"analytics_agent_runs": [("1", 3, 0, 5.0)]}
        rows = analytics_read.get_review_round_breakdown(
            events=["agent_exit", "stage_enter"],
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
