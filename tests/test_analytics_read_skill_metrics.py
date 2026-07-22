# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill-trigger rate and matrix read tests."""

import unittest


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)
from tests.analytics_assertions import (
    assert_row_fields,
    assert_sql_fragments,
)

_APPLY_ONLY_RUNS_ISSUE = 551
_APPLY_ONLY_RUNS_ASSERTIN_ARGUMENT = 551


_STAGE_ENTER = "stage_enter"


_EVENT_AGENT_EXIT = "event = 'agent_exit'"


_EVENT_CATALOG = "event = 'repo_skill_catalog'"


_CLAUDE = "claude"


_CODEX = "codex"


_UNKNOWN = "unknown"


_DEVELOPER = "developer"


_REVIEWER = "reviewer"


_DECOMPOSER = "decomposer"


_REPO = "owner/repo"


_DEVELOP = "develop"


_REVIEW = "review"


_DOCUMENT = "document"


_DEVELOP_ONLY = (_DEVELOP,)


_DEV_CLAUDE_DEVELOP_RUN = (_REPO, _DEVELOPER, _CLAUDE, _DEVELOP_ONLY)


_YEAR = 2026


_WINDOW_START = datetime(_YEAR, 6, 1, tzinfo=timezone.utc)


_WINDOW_END = datetime(_YEAR, 6, 24, tzinfo=timezone.utc)


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
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
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
        assert_row_fields(
            self,
            rows[0],
            {
                "runs": 9,
                "skill_runs": 3,
                "total_triggers": 3,
                "rate": 3 / 9,
            },
        )
        # The quiet reviewer reads as a real 0% trigger rate, not a
        # dropped category.
        assert_row_fields(self, rows[1], {"skill_runs": 0, "rate": float()})
        sql, _ = conn.first_query
        # Skill fields live in `extras` JSONB, which the rollup does
        # not carry, so the reader scans the base table and pins
        # agent_exit directly. Key-presence test (not the jsonb `?`
        # operator) and the trigger-count sum off `extras`.
        assert_sql_fragments(
            self,
            sql,
            (
                "FROM analytics_events",
                _EVENT_AGENT_EXIT,
                "GROUP BY role_label, backend_label",
                "extras -> 'skills_triggered' IS NOT NULL",
                "skills_triggered_count",
            ),
        )

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
            agent_role=_DEVELOPER,
            backend=_CLAUDE,
            runs=0,
        )
        self.assertEqual(rate_row.rate, float())

    def test_window_and_repo_params_bound(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_skill_trigger_rates(
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO,
            connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn(_WINDOW_START, query_params)
        self.assertIn(_REPO, query_params)


class SkillTriggerMatrixRoutingTest(unittest.TestCase):
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
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        # No DB round-trip at all -- not even the catalog query.
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_skill_trigger_matrix(
            events=[],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_window_and_repo_bound_to_both_queries(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_skill_trigger_matrix(
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO,
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
            issue=_APPLY_ONLY_RUNS_ISSUE,
            stages=["implementing"],
            connect=conn.as_connect,
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
        self.assertIn(_APPLY_ONLY_RUNS_ASSERTIN_ARGUMENT, run_params)
        self.assertIn("implementing", run_params)


class SkillTriggerMatrixCellTest(unittest.TestCase):
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
            [(row.skill, row.agent_role, row.backend, row.runs, row.skill_runs) for row in rows],
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
            repo=_REPO,
            skill=_DEVELOP,
            agent_role=_DEVELOPER,
            backend=_CLAUDE,
            runs=4,
            skill_runs=3,
        )
        self.assertEqual(fired.rate, 3 / 4)
        quiet = analytics_read.SkillTriggerMatrixRow(
            repo=_REPO,
            skill=_REVIEW,
            agent_role=_DEVELOPER,
            backend=_CLAUDE,
            runs=4,
            skill_runs=0,
        )
        self.assertEqual(quiet.rate, float())
        empty = analytics_read.SkillTriggerMatrixRow(
            repo=_REPO,
            skill=_DEVELOP,
            agent_role=_DEVELOPER,
            backend=_CLAUDE,
            runs=0,
        )
        self.assertEqual(empty.rate, float())

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
        by_cell = {(row.skill, row.agent_role, row.backend): row.skill_runs for row in rows}
        self.assertEqual(by_cell[(_REVIEW, _DEVELOPER, _CLAUDE)], 0)
        # The triggered-but-uncatalogued skill is still reported, but it
        # is not zero-padded (only catalog skills get zero cells).
        self.assertEqual(by_cell[(_DEVELOP, _DEVELOPER, _CLAUDE)], 1)
        # The zero `skill_runs` cell still reads against its cohort size:
        # the developer/claude cohort ran once, so both cells show runs=1.
        cohort_runs = {(row.skill, row.agent_role, row.backend): row.runs for row in rows}
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
            [(row.skill, row.agent_role, row.backend, row.runs, row.skill_runs) for row in rows],
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


class SkillTriggerMatrixProjectionTest(unittest.TestCase):
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
        by_cell = {(row.skill, row.agent_role, row.backend): row.skill_runs for row in rows}
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
            [(row.skill, row.agent_role, row.backend, row.runs, row.skill_runs) for row in rows],
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
            limit=2,
            connect=conn.as_connect,
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
            limit=0,
            connect=conn.as_connect,
        )
        self.assertEqual(len(all_rows), 3)
