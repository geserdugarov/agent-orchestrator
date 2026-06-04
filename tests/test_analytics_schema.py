# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused schema tests for `analytics-db/init/01-schema.sql`.

The schema is operator-deployed via `docker compose` (or `psql -f`
against an existing instance), so a contract check has to live
outside the live-DDL integration test (which is skipped unless
`ANALYTICS_TEST_DB_URL` is set). These tests assert the SQL text
itself carries the view + indexes the analytics dashboard depends on
so a refactor that accidentally drops them fails in the hermetic
suite -- well before an operator sees a broken dashboard.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "analytics-db" / "init" / "01-schema.sql"
)


def _schema_text() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _normalize(sql: str) -> str:
    """Collapse runs of whitespace so multi-line DDL matches regex."""
    return re.sub(r"\s+", " ", sql).strip()


class SchemaIndexesTest(unittest.TestCase):
    """The dashboard's hot queries rely on these indexes; assert they
    are present and idempotent (`IF NOT EXISTS`) so a re-applied DDL
    against an existing instance does not raise.
    """

    def test_agent_exit_partial_index_present(self) -> None:
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE INDEX IF NOT EXISTS analytics_events_agent_exit_idx "
            r"ON analytics_events\s*\([^)]*\)\s*"
            r"WHERE event = 'agent_exit'",
        )

    def test_stage_enter_partial_index_present(self) -> None:
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE INDEX IF NOT EXISTS analytics_events_stage_enter_idx "
            r"ON analytics_events\s*\([^)]*\)\s*"
            r"WHERE event = 'stage_enter'",
        )

    def test_composite_event_repo_stage_ts_index_present(self) -> None:
        # The column order matters: equality on event / repo / stage
        # then range on ts. A reorder is a behavior change.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE INDEX IF NOT EXISTS analytics_events_event_repo_stage_ts_idx "
            r"ON analytics_events\s*\(\s*event,\s*repo,\s*stage,\s*ts\s*\)",
        )

    def test_indexes_are_idempotent(self) -> None:
        # Every CREATE INDEX in this DDL must be guarded with IF NOT
        # EXISTS so an operator running `psql -f` against an existing
        # instance does not see a duplicate-index error. Catches a
        # future contributor who copy-pastes without the guard.
        text = _schema_text()
        unguarded = re.findall(
            r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX(?!\s+IF\s+NOT\s+EXISTS)",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        self.assertEqual(unguarded, [])


class AnalyticsAgentRunsViewTest(unittest.TestCase):
    """The `analytics_agent_runs` view is the backend shape the
    dashboard / read model consume. These tests pin the columns and
    derivations so a future schema change cannot silently drop a
    field the read model expects.
    """

    def test_view_is_idempotent(self) -> None:
        # CREATE OR REPLACE so re-running the init script (or a
        # `psql -f` against an instance that already has the view)
        # does not error -- mirrors the IF NOT EXISTS guard on
        # tables / indexes.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE OR REPLACE VIEW analytics_agent_runs AS",
        )

    def test_view_filters_to_agent_exit(self) -> None:
        # The view's whole point is to narrow to agent_exit rows so
        # downstream consumers do not have to repeat the predicate.
        text = _normalize(_schema_text())
        # match the view body up to its terminating semicolon
        m = re.search(
            r"CREATE OR REPLACE VIEW analytics_agent_runs AS\s+(.*?);",
            text,
        )
        assert m is not None, "analytics_agent_runs view missing"
        body = m.group(1)
        self.assertRegex(body, r"FROM analytics_events")
        self.assertRegex(body, r"WHERE event = 'agent_exit'")

    def test_view_exposes_required_columns(self) -> None:
        # Pin every column the dashboard / read model wants -- a
        # silently-renamed column would break the dashboard, not the
        # ingest path, so the contract has to live in tests.
        text = _normalize(_schema_text())
        expected_columns = (
            "id", "ts", "repo", "issue", "stage",
            "agent_role", "backend", "agent_spec",
            "resume_session_id", "session_id",
            "review_round", "review_round_bucket",
            "retry_count", "duration_s", "exit_code", "timed_out",
            "failed",
            "input_tokens", "output_tokens", "cached_tokens",
            "cache_read_tokens", "cache_write_tokens",
            "total_tokens", "total_cache_tokens",
            "models", "model",
            "turns",
            "cost_usd", "has_cost", "cost_source",
        )
        for col in expected_columns:
            # AS-aliased derived columns end in ` AS <col>`; plain
            # passthroughs appear as a bare identifier in the select
            # list. Match either form so the assertion does not have
            # to know which is which.
            with self.subTest(column=col):
                pattern = rf"(?:\bAS {col}\b|\b{col}\b)"
                self.assertRegex(text, pattern)

    def test_view_derives_model_from_models_jsonb(self) -> None:
        # The model fallback is `models->>0` with a COALESCE so
        # GROUP BY model never blows up on a NULL key.
        text = _normalize(_schema_text())
        self.assertRegex(text, r"COALESCE\(models->>0,\s*'unknown'\)\s+AS\s+model")

    def test_view_has_cost_is_cost_usd_not_null(self) -> None:
        # The dashboard splits "coverage-known" from "coverage-gap"
        # runs by this flag; keep it tied to cost_usd presence so a
        # cost_source semantics change cannot decouple them.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"\(cost_usd IS NOT NULL\)\s+AS\s+has_cost",
        )

    def test_view_total_tokens_derivation(self) -> None:
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"COALESCE\(input_tokens,\s*0\)\s*\+\s*COALESCE\(output_tokens,\s*0\)"
            r"\s+AS\s+total_tokens",
        )

    def test_view_total_cache_tokens_sums_all_three(self) -> None:
        # cache totals roll up cached + cache_read + cache_write so a
        # dashboard can plot one number; missing one of the three
        # would silently understate the figure.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"COALESCE\(cached_tokens,\s*0\)\s*"
            r"\+\s*COALESCE\(cache_read_tokens,\s*0\)\s*"
            r"\+\s*COALESCE\(cache_write_tokens,\s*0\)\s+"
            r"AS\s+total_cache_tokens",
        )


class AnalyticsDailyRollupViewTest(unittest.TestCase):
    """The `analytics_daily_rollup` materialized view is the pre-aggregated
    target the dashboard reads from instead of scanning the raw run
    tables. These tests pin the create statement's idempotency, the key
    columns, and the aggregate columns so a refactor that drops the
    timeout/failure counts or token sums (which the dashboard's
    reliability tiles and KPI strip read from) fails in the hermetic
    suite -- before an operator sees a broken dashboard.
    """

    def _view_body(self) -> str:
        # Materialized views terminate at the SELECT's semicolon; isolate
        # the body so column-presence assertions cannot accidentally
        # match text from the surrounding `analytics_agent_runs` view or
        # the index DDL.
        text = _normalize(_schema_text())
        match = re.search(
            r"CREATE MATERIALIZED VIEW IF NOT EXISTS "
            r"analytics_daily_rollup AS\s+(.*?);",
            text,
        )
        assert match is not None, "analytics_daily_rollup view missing"
        return match.group(1)

    def test_view_is_idempotent_create(self) -> None:
        # `CREATE MATERIALIZED VIEW IF NOT EXISTS` matches the
        # idempotency contract every other CREATE in this DDL upholds:
        # an operator running `psql -f` against an existing instance
        # picks up the view on first apply and no-ops on every reapply.
        # Postgres CREATE MATERIALIZED VIEW does not support OR REPLACE,
        # so IF NOT EXISTS is the only available guard.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE MATERIALIZED VIEW IF NOT EXISTS analytics_daily_rollup",
        )

    def test_view_reads_from_analytics_events(self) -> None:
        body = self._view_body()
        self.assertRegex(body, r"FROM analytics_events")

    def test_view_groups_by_required_key_columns(self) -> None:
        # The key has to include `issue` because every dashboard read
        # accepts an issue filter; without it an issue-scoped read
        # would double-count. `cost_source` is in the key so the
        # cost-coverage panel can read from the rollup without
        # decomposing the `unknown-price` / `no-usage` / `reported` /
        # `estimated` cohorts after the fact.
        body = self._view_body()
        for key_col in (
            "repo", "issue", "event", "stage", "backend", "cost_source",
        ):
            with self.subTest(column=key_col):
                # GROUP BY column listed verbatim; the day expression
                # is asserted separately because it carries a cast.
                self.assertRegex(body, rf"\b{key_col}\b")
        # `day` is derived from `ts AT TIME ZONE 'UTC'`::date -- the
        # cast normalises naive / non-UTC timestamps so the rollup is
        # consistent across writers.
        self.assertRegex(
            body, r"\(ts AT TIME ZONE 'UTC'\)::date\s+AS\s+day",
        )

    def test_view_exposes_required_aggregate_columns(self) -> None:
        # Every aggregate the dashboard / read model wants to read off
        # the rollup must be present. A silently-dropped aggregate
        # would force a fallback to the raw events table, which is
        # what Layer 4 exists to avoid.
        body = self._view_body()
        for col in (
            "total_input_tokens",
            "total_output_tokens",
            "total_cached_tokens",
            "total_cache_read_tokens",
            "total_cache_write_tokens",
            "total_cost_usd",
            "duration_s_sum",
            "duration_s_count",
            "failed_count",
            "timed_out_count",
            "event_count",
        ):
            with self.subTest(column=col):
                self.assertRegex(body, rf"\bAS\s+{col}\b")

    def test_view_duration_count_uses_not_null_filter(self) -> None:
        # `duration_s_count` is the row count where duration_s is
        # populated -- not the raw row count. Without that, a consumer
        # recovering `AVG(duration_s)` as `SUM/COUNT` would divide by
        # the wrong denominator on rows where duration was NULL.
        body = self._view_body()
        self.assertRegex(
            body,
            r"SUM\(CASE WHEN duration_s IS NOT NULL THEN 1 ELSE 0 END\)\s+"
            r"AS\s+duration_s_count",
        )

    def test_view_failed_count_filters_to_non_zero_exit_code(self) -> None:
        body = self._view_body()
        # Non-zero exit_code is the failure signal; NULL exit_code
        # stays excluded so a `stage_enter` row never counts as a
        # failure.
        self.assertRegex(
            body,
            r"SUM\(CASE WHEN exit_code IS NOT NULL AND exit_code <> 0 "
            r"THEN 1 ELSE 0 END\)\s+AS\s+failed_count",
        )

    def test_view_timed_out_count_filters_to_agent_exit(self) -> None:
        body = self._view_body()
        # The reliability "Timeouts" tile reads this aggregate; it must
        # be scoped to `event='agent_exit'` so a `stage_enter` row with
        # a stale `timed_out` JSONB extra (impossible today, but the
        # filter is the defensive layer) can never inflate the count.
        self.assertRegex(
            body,
            r"SUM\(CASE WHEN event = 'agent_exit' AND timed_out = TRUE "
            r"THEN 1 ELSE 0 END\)\s+AS\s+timed_out_count",
        )

    def test_view_event_count_is_row_count(self) -> None:
        body = self._view_body()
        self.assertRegex(body, r"COUNT\(\*\)\s+AS\s+event_count")

    def test_unique_key_index_present_with_nulls_not_distinct(self) -> None:
        # `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a unique
        # index. NULLS NOT DISTINCT (Postgres 15+) collapses NULL
        # stage / backend / cost_source values into one row -- the same
        # way GROUP BY already does -- so the index is genuinely
        # unique across the view's contents. The current sync uses the
        # non-concurrent variant, so the index is forward-compat
        # plumbing rather than load-bearing today.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE UNIQUE INDEX IF NOT EXISTS "
            r"analytics_daily_rollup_key_idx\s+"
            r"ON analytics_daily_rollup\s*"
            r"\(\s*day,\s*repo,\s*issue,\s*event,\s*stage,\s*backend,"
            r"\s*cost_source\s*\)\s+NULLS NOT DISTINCT",
        )

    def test_supporting_day_repo_index_present(self) -> None:
        # Day-range scan support for the dashboard's window-bounded
        # reads. Without this, a `WHERE day BETWEEN x AND y` predicate
        # would fall back to a sequential scan over the rollup once it
        # grew past a few thousand rows.
        text = _normalize(_schema_text())
        self.assertRegex(
            text,
            r"CREATE INDEX IF NOT EXISTS "
            r"analytics_daily_rollup_day_repo_idx\s+"
            r"ON analytics_daily_rollup\s*\(\s*day,\s*repo\s*\)",
        )


if __name__ == "__main__":
    unittest.main()
