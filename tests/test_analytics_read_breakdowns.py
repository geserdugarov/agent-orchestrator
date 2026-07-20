# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# Event / stage predicates and table names the breakdown readers scan.
_STAGE_ENTER = "stage_enter"
_AGENT_RUNS_VIEW = "analytics_agent_runs"
_ROLLUP_SCAN = "FROM analytics_daily_rollup"
_EVENT_AGENT_EXIT = "event = 'agent_exit'"
_EVENT_CATALOG = "event = 'repo_skill_catalog'"

# Backend / agent-role / skill / repo labels the fixtures thread
# through the readers.
_CLAUDE = "claude"
_CODEX = "codex"
_UNKNOWN = "unknown"
_UNKNOWN_PRICE = "unknown-price"
_DEVELOPER = "developer"
_REVIEWER = "reviewer"
_DECOMPOSER = "decomposer"
_REPO = "owner/repo"
_DEVELOP = "develop"
_REVIEW = "review"
_DOCUMENT = "document"
# A single-skill `skills_available` / `skills_triggered` array. The
# reader only reads it, so an immutable tuple is safe; `_as_skill_names`
# accepts either a list or a tuple.
_DEVELOP_ONLY = (_DEVELOP,)
# A developer/claude `agent_exit` run that fired the `develop` skill: the
# matrix-fixture row threaded through the skill-rate readers.
_DEV_CLAUDE_DEVELOP_RUN = (_REPO, _DEVELOPER, _CLAUDE, _DEVELOP_ONLY)

# Reused rollup days and window bounds. The year is pinned so the
# fixture timestamps stay stable; the day / month components are the
# assertion surface rather than incidental fixture noise.
_YEAR = 2026
_DAY_ONE = date(_YEAR, 5, 1)
_DAY_TWO = date(_YEAR, 5, 2)
_WINDOW_START = datetime(_YEAR, 6, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 6, 24, tzinfo=timezone.utc)


class BackendDailyTokensTest(unittest.TestCase):
    """`get_backend_daily_tokens` powers the redesigned dashboard's
    "By backend" hero toggle. It must read from the view, honor the
    agent-run event-filter short-circuit, and aggregate tokens across
    every agent run in the window (not a `LIMIT`-capped subset).
    """

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_backend_daily_tokens(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_reads_daily_backend_totals_from_view(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                (_DAY_ONE, _CLAUDE, 12_000),
                (_DAY_ONE, _CODEX, 4_500),
                (_DAY_TWO, _CLAUDE, 8_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(connect=conn.as_connect)
        self.assertEqual(
            [(row.day, row.backend, row.total_tokens) for row in rows],
            [
                (_DAY_ONE, _CLAUDE, 12_000),
                (_DAY_ONE, _CODEX, 4_500),
                (_DAY_TWO, _CLAUDE, 8_000),
            ],
        )
        sql, _ = conn.first_query
        # Reads from the view -- so the agent-run filter contract
        # (no `event IN` clause) holds -- and groups by both day and
        # backend so the dashboard can build a per-day stack without
        # post-processing. Token total includes the cache band so the
        # backend stack matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("event IN", sql)
        self.assertIn("GROUP BY day, backend_label", sql)
        for token_column in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(token_column, sql)

    def test_null_backend_buckets_under_unknown(self) -> None:
        # `COALESCE(backend, 'unknown')` matches how
        # `get_backend_efficiency` surfaces NULL-backend rows.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                (_DAY_ONE, _UNKNOWN, 1_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(connect=conn.as_connect)
        self.assertEqual([row.backend for row in rows], [_UNKNOWN])


class BackendEfficiencyTest(unittest.TestCase):
    """`get_backend_efficiency` aggregates the agent_runs view by
    backend and exposes failure / cost / token rollups."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_backend_efficiency(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 9-tuple: backend / runs / failed / avg_dur / cost /
        # input_tokens / output_tokens / cache_read / cache_write. The
        # reader reads the daily rollup (with `event = 'agent_exit'`
        # pinned to match the agent-runs view filter); the fixture
        # pre-computes the weighted average so the reader's NULL
        # handling still rides through. Cache columns feed the
        # per-backend "cost / 1M tok" tile alongside input + output.
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_CLAUDE, 20, 1, 35, 1.2, 5000, 4000, 1500, 800),
                (_CODEX, 10, 3, None, 0.4, 1000, 2000, 0, 0),
                (_UNKNOWN, 1, 0, None, 0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=conn.as_connect)
        self.assertEqual(
            [row.backend for row in rows], [_CLAUDE, _CODEX, _UNKNOWN],
        )
        self.assertEqual(rows[0].runs, 20)
        self.assertEqual(rows[0].failed, 1)
        self.assertEqual(rows[0].avg_duration_s, 35)
        self.assertEqual(rows[0].total_cost_usd, 1.2)
        self.assertEqual(rows[0].total_input_tokens, 5000)
        self.assertEqual(rows[0].total_output_tokens, 4000)
        # Cache columns feed the per-backend "cost / 1M tok" tile
        # alongside input + output.
        self.assertEqual(rows[0].total_cache_read_tokens, 1500)
        self.assertEqual(rows[0].total_cache_write_tokens, 800)
        # NULL avg duration preserved so the dashboard can hide the
        # column rather than show a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.first_query
        # The rollup carries an `event` column, so the cutover query
        # pins `event = 'agent_exit'` directly rather than the view's
        # implicit filter. Weighted-duration recovery comes from the
        # rollup's duration sums, not `AVG(duration_s)` over the raw
        # events table.
        for fragment in (
            _ROLLUP_SCAN,
            _EVENT_AGENT_EXIT,
            "COALESCE(backend, 'unknown')",
            "SUM(total_cache_read_tokens)",
            "SUM(total_cache_write_tokens)",
            "SUM(duration_s_sum)",
            "NULLIF(SUM(duration_s_count), 0)",
        ):
            self.assertIn(fragment, sql)

    def test_seven_tuple_defaults_cache_to_zero(self) -> None:
        # Older 7-tuple `(backend, runs, failed, avg_dur, cost, in,
        # out)` rows still round-trip with zero cache tokens so
        # unrelated tests keep working.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_CLAUDE, 5, 0, 10, 0.2, 1000, 500),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=conn.as_connect)
        self.assertEqual(rows[0].total_cache_read_tokens, 0)
        self.assertEqual(rows[0].total_cache_write_tokens, 0)


class SkillTriggerRatesTest(unittest.TestCase):
    """`get_skill_trigger_rates` aggregates the base `analytics_events`
    table by `(agent_role, backend)` over the `extras` JSONB skill
    fields, honoring the same `agent_exit` event-filter contract as
    `get_backend_efficiency`."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_skill_trigger_rates(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_skill_trigger_rates(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        # No DB round-trip when the events filter excludes agent_exit.
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # (agent_role, backend, runs, skill_runs, total_triggers) --
        # mirrors the live-data table in the design doc.
        conn.rows_for = {
            "GROUP BY role_label, backend_label": [
                (_DEVELOPER, _CLAUDE, 9, 3, 3),
                (_REVIEWER, _CODEX, 5, 0, 0),
                (_DECOMPOSER, _CODEX, 2, 0, 0),
            ],
        }
        rows = analytics_read.get_skill_trigger_rates(connect=conn.as_connect)
        self.assertEqual(
            [(row.agent_role, row.backend) for row in rows],
            [
                (_DEVELOPER, _CLAUDE),
                (_REVIEWER, _CODEX),
                (_DECOMPOSER, _CODEX),
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
        sql, _ = conn.first_query
        # Skill fields live in `extras` JSONB, which the rollup does
        # not carry, so the reader scans the base table and pins
        # agent_exit directly. Key-presence test (not the jsonb `?`
        # operator) and the trigger-count sum off `extras`.
        for fragment in (
            "FROM analytics_events",
            _EVENT_AGENT_EXIT,
            "GROUP BY role_label, backend_label",
            "extras -> 'skills_triggered' IS NOT NULL",
            "skills_triggered_count",
        ):
            self.assertIn(fragment, sql)

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # COALESCE maps NULL -> 'unknown' in SQL; the reader also
        # guards None defensively so a fake row without COALESCE still
        # round-trips.
        conn.rows_for = {
            "GROUP BY role_label, backend_label": [
                (None, None, 4, 0, 0),
            ],
        }
        rows = analytics_read.get_skill_trigger_rates(connect=conn.as_connect)
        top_row = rows[0]
        self.assertEqual(top_row.agent_role, _UNKNOWN)
        self.assertEqual(top_row.backend, _UNKNOWN)

    def test_rate_zero_runs_does_not_divide(self) -> None:
        # Defensive: a zero-run group (never emitted by the SQL) still
        # yields 0.0 rather than a ZeroDivisionError. No DB is touched --
        # the row is constructed directly.
        analytics_read = _reload_read()
        rate_row = analytics_read.SkillTriggerRateRow(
            agent_role=_DEVELOPER, backend=_CLAUDE, runs=0,
        )
        self.assertEqual(rate_row.rate, 0.0)

    def test_window_and_repo_params_bound(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_skill_trigger_rates(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO,
            connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn(_WINDOW_START, query_params)
        self.assertIn(_REPO, query_params)


class SkillTriggerMatrixTest(unittest.TestCase):
    """`get_skill_trigger_matrix` combines the `repo_skill_catalog`
    records (the offered-skill universe) with the filtered `agent_exit`
    rows (the runs that fired a skill) into a per-skill x
    `(repo, agent_role, backend)` matrix, honoring the same
    `agent_exit` event-filter contract as `get_skill_trigger_rates`.

    Coverage spans the short-circuit contract, the observed /
    catalog-padded cell values and the `rate` property, and the
    window / filter binding, NULL bucketing, JSON coercion, cohort run
    counts, sort order, and row-count cap.
    """

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_skill_trigger_matrix(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_skill_trigger_matrix(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        # No DB round-trip at all -- not even the catalog query.
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_skill_trigger_matrix(
            events=[], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_observed_and_zero_cells_round_trip(self) -> None:
        conn = _FakeConnection()
        # Catalog query -> `(repo, skills_available)`; runs query ->
        # `(repo, role_label, backend_label, skills_triggered)`. psycopg
        # adapts the JSONB arrays to Python lists, so the fixture mirrors
        # that. The two queries pin distinct event kinds, so the fake
        # cursor routes each to its own rows.
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, [_DEVELOP, _REVIEW]),
            ],
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
                _DEV_CLAUDE_DEVELOP_RUN,
                (_REPO, _REVIEWER, _CODEX, [_REVIEW]),
                # A tracked run that fired nothing still defines its
                # cohort, so the cohort gets zero-padded catalog cells.
                (_REPO, _DEVELOPER, _CLAUDE, None),
            ],
        }
        rows = _reload_read().get_skill_trigger_matrix(connect=conn.as_connect)
        # Ordered by skill_runs DESC, then cohort runs DESC, then the
        # stable (repo, role, backend, skill) tiebreak. developer/claude
        # ran three times (two `develop`, one that fired nothing),
        # reviewer/codex once.
        self.assertEqual(
            [
                (row.skill, row.agent_role, row.backend, row.runs, row.skill_runs)
                for row in rows
            ],
            [
                (_DEVELOP, _DEVELOPER, _CLAUDE, 3, 2),
                (_REVIEW, _REVIEWER, _CODEX, 1, 1),
                (_REVIEW, _DEVELOPER, _CLAUDE, 3, 0),
                (_DEVELOP, _REVIEWER, _CODEX, 1, 0),
            ],
        )
        self.assertEqual({row.repo for row in rows}, {_REPO})
        # Catalog query first, runs query second; both scan the base
        # table for the JSONB arrays the rollup does not carry.
        cat_sql, _ = conn.executed[0]
        run_sql, _ = conn.executed[1]
        self.assertIn(_EVENT_CATALOG, cat_sql)
        self.assertIn("extras -> 'skills_available'", cat_sql)
        self.assertIn(_EVENT_AGENT_EXIT, run_sql)
        self.assertIn("extras -> 'skills_triggered'", run_sql)
        for scan_sql in (cat_sql, run_sql):
            self.assertIn("FROM analytics_events", scan_sql)
            # Neither query touches the rollup / agent-runs view.
            self.assertNotIn("analytics_daily_rollup", scan_sql)
            self.assertNotIn("analytics_agent_runs", scan_sql)

    def test_rate_handles_counts_and_zero_runs(self) -> None:
        # `rate` is `skill_runs / runs` (the offered-but-quiet cell reads
        # `0.0`), and a zero-run cell -- never emitted by the SQL -- still
        # yields `0.0` rather than a ZeroDivisionError. No DB is touched --
        # the rows are constructed directly.
        analytics_read = _reload_read()
        fired = analytics_read.SkillTriggerMatrixRow(
            repo=_REPO, skill=_DEVELOP, agent_role=_DEVELOPER,
            backend=_CLAUDE, runs=4, skill_runs=3,
        )
        self.assertEqual(fired.rate, 3 / 4)
        quiet = analytics_read.SkillTriggerMatrixRow(
            repo=_REPO, skill=_REVIEW, agent_role=_DEVELOPER,
            backend=_CLAUDE, runs=4, skill_runs=0,
        )
        self.assertEqual(quiet.rate, 0.0)
        empty = analytics_read.SkillTriggerMatrixRow(
            repo=_REPO, skill=_DEVELOP, agent_role=_DEVELOPER,
            backend=_CLAUDE, runs=0,
        )
        self.assertEqual(empty.rate, 0.0)

    def test_developer_claude_review_is_zero(self) -> None:
        # A skill the repo offers that a running cohort never triggered
        # surfaces as an explicit 0, not a missing row.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, [_REVIEW]),
            ],
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        by_cell = {
            (row.skill, row.agent_role, row.backend): row.skill_runs for row in rows
        }
        self.assertEqual(by_cell[(_REVIEW, _DEVELOPER, _CLAUDE)], 0)
        # The triggered-but-uncatalogued skill is still reported, but it
        # is not zero-padded (only catalog skills get zero cells).
        self.assertEqual(by_cell[(_DEVELOP, _DEVELOPER, _CLAUDE)], 1)
        # The zero `skill_runs` cell still reads against its cohort size:
        # the developer/claude cohort ran once, so both cells show runs=1.
        cohort_runs = {
            (row.skill, row.agent_role, row.backend): row.runs for row in rows
        }
        self.assertEqual(cohort_runs[(_REVIEW, _DEVELOPER, _CLAUDE)], 1)
        self.assertEqual(cohort_runs[(_DEVELOP, _DEVELOPER, _CLAUDE)], 1)

    def test_missing_catalog_falls_back_to_observed(self) -> None:
        # No `repo_skill_catalog` rows match -> the catalog query returns
        # nothing, so the matrix degrades to just the observed-trigger
        # cells without inventing zero rows or raising.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
                # A cohort that triggered nothing contributes no cells
                # at all when there is no catalog to pad against.
                (_REPO, _REVIEWER, _CODEX, None),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        self.assertEqual(
            [
                (row.skill, row.agent_role, row.backend, row.runs, row.skill_runs)
                for row in rows
            ],
            [(_DEVELOP, _DEVELOPER, _CLAUDE, 1, 1)],
        )
        # Both queries still ran (catalog returned empty from the fake).
        self.assertEqual(len(conn.executed), 2)

    def test_question_decompose_cohorts_are_empty(self) -> None:
        # decomposer / question runs emit `agent_exit` just like
        # developer / reviewer, so their cohorts must be zero-padded with
        # the repo's catalog skills even when they trigger nothing.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, _DEVELOP_ONLY),
            ],
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
                # decomposer / question ran but fired no cataloged skill.
                (_REPO, _DECOMPOSER, _CLAUDE, None),
                (_REPO, "question", _CODEX, None),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        by_cell = {
            (row.skill, row.agent_role, row.backend): (row.runs, row.skill_runs)
            for row in rows
        }
        # Both roles surface as catalog-backed zero rows (skill_runs=0)
        # against their real cohort size, the same way developer does.
        self.assertEqual(by_cell[(_DEVELOP, _DECOMPOSER, _CLAUDE)], (1, 0))
        self.assertEqual(by_cell[(_DEVELOP, "question", _CODEX)], (1, 0))
        self.assertEqual(by_cell[(_DEVELOP, _DEVELOPER, _CLAUDE)], (1, 1))

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, _DEVELOP_ONLY),
            ],
            _EVENT_AGENT_EXIT: [
                (_REPO, None, None, _DEVELOP_ONLY),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_role, _UNKNOWN)
        self.assertEqual(rows[0].backend, _UNKNOWN)
        self.assertEqual(rows[0].runs, 1)
        self.assertEqual(rows[0].skill_runs, 1)
        # COALESCE maps NULL -> 'unknown' in SQL too, so the reader and
        # the query agree even before the Python-side guard runs.
        run_sql, _ = conn.executed[1]
        self.assertIn("COALESCE(agent_role, 'unknown')", run_sql)
        self.assertIn("COALESCE(backend, 'unknown')", run_sql)

    def test_window_and_repo_bound_to_both_queries(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_skill_trigger_matrix(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO,
            connect=conn.as_connect,
        )
        # Date + repo narrow BOTH the catalog and the runs query.
        for sql, query_params in conn.executed:
            self.assertIn("ts >= %s", sql)
            self.assertIn("ts < %s", sql)
            self.assertIn("repo = %s", sql)
            self.assertIn(_WINDOW_START, query_params)
            self.assertIn(_REPO, query_params)

    def test_issue_stage_filters_apply_only_to_runs(self) -> None:
        # The stage / issue filters narrow only the agent_exit runs;
        # pushing them onto the repo-level catalog records (issue == 0,
        # NULL stage) would drop every catalog row.
        conn = _FakeConnection()
        _reload_read().get_skill_trigger_matrix(
            issue=551, stages=["implementing"], connect=conn.as_connect,
        )
        cat_sql, cat_params = conn.executed[0]
        run_sql, run_params = conn.executed[1]
        self.assertNotIn("issue = %s", cat_sql)
        self.assertNotIn("stage IN", cat_sql)
        # No window filter applies to the repo-level catalog, so its
        # query binds no parameters (the fake records them as a tuple).
        self.assertEqual(cat_params, ())
        self.assertIn("issue = %s", run_sql)
        self.assertIn("stage IN", run_sql)
        self.assertIn(551, run_params)
        self.assertIn("implementing", run_params)

    def test_skill_names_coerced_from_json_text(self) -> None:
        # Defensive: a driver / fixture that returns the JSONB arrays as
        # raw JSON text (rather than adapted Python lists) still parses.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, '["develop", "review"]'),
            ],
            _EVENT_AGENT_EXIT: [
                (_REPO, _DEVELOPER, _CLAUDE, '["develop"]'),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        by_cell = {
            (row.skill, row.agent_role, row.backend): row.skill_runs for row in rows
        }
        self.assertEqual(by_cell[(_DEVELOP, _DEVELOPER, _CLAUDE)], 1)
        self.assertEqual(by_cell[(_REVIEW, _DEVELOPER, _CLAUDE)], 0)

    def test_run_count_includes_runs_without_skills(self) -> None:
        # `runs` is the cohort total: a cohort with four runs, only one
        # of which fired the skill, reads runs=4 / skill_runs=1 so the low
        # trigger count is legible against the cohort size.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, _DEVELOP_ONLY),
            ],
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
                (_REPO, _DEVELOPER, _CLAUDE, None),
                (_REPO, _DEVELOPER, _CLAUDE, []),
                (_REPO, _DEVELOPER, _CLAUDE, [_REVIEW]),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        by_skill = {row.skill: row for row in rows}
        # The cohort ran four times; `develop` triggered on one of them.
        self.assertEqual(by_skill[_DEVELOP].runs, 4)
        self.assertEqual(by_skill[_DEVELOP].skill_runs, 1)
        # The triggered-but-uncatalogued `review` skill shares the cohort
        # total too -- four cohort runs, one trigger.
        self.assertEqual(by_skill[_REVIEW].runs, 4)
        self.assertEqual(by_skill[_REVIEW].skill_runs, 1)

    def test_sorted_by_skill_then_cohort_run_count(self) -> None:
        # Acceptance order: Runs-with-skill DESC, then cohort Runs DESC,
        # then a stable repo/role/backend/skill tiebreak.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, [_DEVELOP, _REVIEW]),
            ],
            _EVENT_AGENT_EXIT: [
                # cohort developer/claude: 3 runs, `develop` fired twice.
                _DEV_CLAUDE_DEVELOP_RUN,
                _DEV_CLAUDE_DEVELOP_RUN,
                (_REPO, _DEVELOPER, _CLAUDE, None),
                # cohort reviewer/codex: 2 runs, `develop` fired twice.
                # Same skill_runs as developer/claude's `develop` but a
                # smaller cohort, so it sorts after on the Runs DESC
                # tiebreak.
                (_REPO, _REVIEWER, _CODEX, _DEVELOP_ONLY),
                (_REPO, _REVIEWER, _CODEX, _DEVELOP_ONLY),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(connect=conn.as_connect)
        self.assertEqual(
            [
                (row.skill, row.agent_role, row.backend, row.runs, row.skill_runs)
                for row in rows
            ],
            [
                # skill_runs=2, tied -> larger cohort first.
                (_DEVELOP, _DEVELOPER, _CLAUDE, 3, 2),
                (_DEVELOP, _REVIEWER, _CODEX, 2, 2),
                # skill_runs=0 catalog-padded `review`, larger cohort first.
                (_REVIEW, _DEVELOPER, _CLAUDE, 3, 0),
                (_REVIEW, _REVIEWER, _CODEX, 2, 0),
            ],
        )

    def test_row_count_capped_at_limit(self) -> None:
        # The list is capped (default 100); a smaller `limit` keeps the
        # highest-weight rows in the sorted order and drops the tail.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _EVENT_CATALOG: [
                (_REPO, [_DEVELOP, _REVIEW, _DOCUMENT]),
            ],
            _EVENT_AGENT_EXIT: [
                _DEV_CLAUDE_DEVELOP_RUN,
                (_REPO, _DEVELOPER, _CLAUDE, [_REVIEW]),
                (_REPO, _DEVELOPER, _CLAUDE, [_REVIEW]),
            ],
        }
        rows = analytics_read.get_skill_trigger_matrix(
            limit=2, connect=conn.as_connect,
        )
        # Three catalog cells exist (develop=1, review=2, document=0) but
        # only the top two by skill_runs survive: review (2) then develop
        # (1).
        self.assertEqual(
            [(row.skill, row.skill_runs) for row in rows],
            [(_REVIEW, 2), (_DEVELOP, 1)],
        )
        # A non-positive limit disables the cap -- all three cells return.
        all_rows = analytics_read.get_skill_trigger_matrix(
            limit=0, connect=conn.as_connect,
        )
        self.assertEqual(len(all_rows), 3)


class RepoBreakdownTest(unittest.TestCase):
    """`get_repo_breakdown` reads the base table so the standard
    event/stage/date/repo/issue filter shape applies (no agent_runs
    short-circuit)."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_repo_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_per_repo_rows(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY repo": [
                ("owner/a", 5, 30, 4, 0.5),
                ("owner/b", 2, 10, 1, 0.1),
            ],
        }
        rows = analytics_read.get_repo_breakdown(connect=conn.as_connect)
        self.assertEqual(rows[0].repo, "owner/a")
        self.assertEqual(rows[0].issues, 5)
        self.assertEqual(rows[0].events, 30)
        self.assertEqual(rows[0].agent_exits, 4)
        self.assertEqual(rows[0].total_cost_usd, 0.5)
        sql, _ = conn.first_query
        # GROUP BY repo with distinct issue count per row -- safe
        # because rollup rows are already scoped to one repo per bucket
        # and the rollup key carries `issue`.
        self.assertIn("COUNT(DISTINCT issue)", sql)
        self.assertIn(_ROLLUP_SCAN, sql)

    def test_event_filter_threaded(self) -> None:
        # `get_repo_breakdown` honors the standard event filter because
        # it reads the base table (which carries an `event` column).
        # Cleared multiselect -> FALSE predicate.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_repo_breakdown(events=[], connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("FALSE", sql)


class CostCoverageTest(unittest.TestCase):
    """`get_cost_coverage` MUST keep `unknown-price` visible -- it is
    the maintenance signal for the pricing table in
    `orchestrator.usage`. Distinct from rows whose `cost_source` is
    NULL, which bucket under the generic `"unknown"`."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_cost_coverage(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_cost_coverage(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_unknown_price_preserved_verbatim(self) -> None:
        conn = _FakeConnection()
        # The third tuple column is the per-`cost_source` token rollup
        # that feeds the redesigned token-share coverage bar.
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                ("reported", 20, 800_000),
                ("estimated", 5, 100_000),
                (_UNKNOWN_PRICE, 3, 60_000),
                ("no-usage", 2, 20_000),
                (_UNKNOWN, 1, 5_000),
            ],
        }
        rows = _reload_read().get_cost_coverage(connect=conn.as_connect)
        by_source = {row.cost_source: row for row in rows}
        # The `unknown-price` slice surfaces with that exact label --
        # NEVER folded into "unknown" -- so the operator can see which
        # runs the parser could not price.
        self.assertIn(_UNKNOWN_PRICE, by_source)
        self.assertEqual(
            sum(1 for row in rows if row.cost_source == _UNKNOWN_PRICE), 1,
        )
        self.assertEqual(
            sum(1 for row in rows if row.cost_source == _UNKNOWN), 1,
        )
        # Per-source token volume rolls up alongside the run count.
        self.assertEqual(by_source["reported"].total_tokens, 800_000)
        self.assertEqual(by_source[_UNKNOWN_PRICE].total_tokens, 60_000)
        sql, _ = conn.first_query
        self.assertIn("FROM analytics_agent_runs", sql)
        # NULL cost_source rows bucket under "unknown" via COALESCE, but
        # the verbatim "unknown-price" string is untouched. SQL totals
        # input + output + cache_read + cache_write so the token share
        # matches the standalone mock's accounting.
        self.assertIn("COALESCE(cost_source, 'unknown')", sql)
        for token_column in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(token_column, sql)

    def test_legacy_two_tuple_defaults_tokens_to_zero(self) -> None:
        # Older 2-tuple `(cost_source, runs)` rows still round-trip; the
        # reader defaults `total_tokens` to zero so unrelated tests
        # round-trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [("reported", 3)],
        }
        rows = analytics_read.get_cost_coverage(connect=conn.as_connect)
        self.assertEqual([row.total_tokens for row in rows], [0])


class ReviewRoundBreakdownTest(unittest.TestCase):
    """`get_review_round_breakdown` reads from `analytics_agent_runs`
    so the agent-run filter contract (no `event` column in the view)
    is encoded as a Python-side short-circuit on `_agent_event_excluded`."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_review_round_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_view_query_buckets_rounds(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 12-tuple rows carry the role + cache split the chart consumes:
        # (bucket, runs, failed, cost, dev_runs, rev_runs, dev_cost,
        # rev_cost, dev_cache, dev_no_cache, rev_cache, rev_no_cache).
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                ("0", 12, 1, 40, 7, 5, 28, 12, 20, 8, 9, 3),
                ("1", 8, 2, 25, 4, 4, 10, 15, 7, 3, 11, 4),
                ("3-5", 4, 4, 18, 1, 3, 5, 13, 5, 0, 13, 0),
                (_UNKNOWN, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_review_round_breakdown(connect=conn.as_connect)
        # Each column is checked independently against its own expected
        # values. `total_cost_usd` powers the "Cost by review round"
        # chart and "Rework share" tile; the per-role cache vs no-cache
        # split stacks so cache_cost + no_cache_cost equals the total.
        for field, expected in (
            ("bucket", ["0", "1", "3-5", _UNKNOWN]),
            ("runs", [12, 8, 4, 1]),
            ("failed", [1, 2, 4, 0]),
            ("total_cost_usd", [40, 25, 18, 0]),
            ("developer_runs", [7, 4, 1, 1]),
            ("reviewer_runs", [5, 4, 3, 0]),
            ("developer_cost_usd", [28, 10, 5, 0]),
            ("reviewer_cost_usd", [12, 15, 13, 0]),
            ("developer_cache_cost_usd", [20, 7, 5, 0]),
            ("developer_no_cache_cost_usd", [8, 3, 0, 0]),
            ("reviewer_cache_cost_usd", [9, 11, 13, 0]),
            ("reviewer_no_cache_cost_usd", [3, 4, 0, 0]),
        ):
            self.assertEqual([getattr(row, field) for row in rows], expected, field)
        sql, _ = conn.first_query
        # Reads from the view (no `event` column, so no `event IN`
        # clause). The cache / no-cache split is proportional: each
        # run's cost is weighted by the cache-token share of its
        # billable token volume. Codex `cached_tokens` is already a
        # subset of `input_tokens`, so it appears in the numerator only.
        for fragment in (
            "FROM analytics_agent_runs",
            "SUM(cost_usd)",
            "agent_role IN ('developer', 'reviewer')",
            "agent_role = 'developer'",
            "agent_role = 'reviewer'",
            "stage = 'implementing' THEN '0'",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "developer_cache_cost_usd",
            "developer_no_cache_cost_usd",
            "reviewer_cache_cost_usd",
            "reviewer_no_cache_cost_usd",
        ):
            self.assertIn(fragment, sql)
        self.assertNotIn("event IN", sql)

    def test_legacy_three_tuple_defaults_cost_to_zero(self) -> None:
        # Older 3-tuple `(bucket, runs, failed)` rows without the cost /
        # role / cache rollups still round-trip with those values
        # defaulted to zero.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_AGENT_RUNS_VIEW: [("0", 3, 0)]}
        rows = analytics_read.get_review_round_breakdown(connect=conn.as_connect)
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
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_AGENT_RUNS_VIEW: [("1", 3, 0, 5)]}
        rows = analytics_read.get_review_round_breakdown(
            events=["agent_exit", _STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
