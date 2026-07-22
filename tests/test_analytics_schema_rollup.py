# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics daily-rollup definition, aggregation, and index tests."""

import re


import unittest


from pathlib import Path


_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "analytics-db" / "init"


_SCHEMA_PATH = _SCHEMA_DIR / "01-schema.sql"


def _schema_text() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _normalize(sql: str) -> str:
    """Collapse runs of whitespace so multi-line DDL matches regex."""
    return re.sub(r"\s+", " ", sql).strip()


def _normalized_schema() -> str:
    """Whitespace-collapsed schema DDL, shared across the contract tests."""
    return _normalize(_schema_text())


def _rollup_view_body() -> str:
    # Materialized views terminate at the SELECT's semicolon; isolate
    # the body so column-presence assertions cannot accidentally
    # match text from the surrounding `analytics_agent_runs` view or
    # the index DDL.
    match = re.search(
        r"CREATE MATERIALIZED VIEW IF NOT EXISTS "
        r"analytics_daily_rollup AS\s+(.*?);",
        _normalized_schema(),
    )
    assert match is not None, "analytics_daily_rollup view missing"
    return match.group(1)


class AnalyticsDailyRollupDefinitionTest(unittest.TestCase):
    """The `analytics_daily_rollup` materialized view is the pre-aggregated
    target the dashboard reads from instead of scanning the raw run
    tables. These tests pin the create statement's idempotency, the key
    columns, and the aggregate columns so a refactor that drops the
    timeout/failure counts or token sums (which the dashboard's
    reliability tiles and KPI strip read from) fails in the hermetic
    suite -- before an operator sees a broken dashboard.
    """

    def test_view_is_idempotent_create(self) -> None:
        # `CREATE MATERIALIZED VIEW IF NOT EXISTS` matches the
        # idempotency contract every other CREATE in this DDL upholds:
        # an operator running `psql -f` against an existing instance
        # picks up the view on first apply and no-ops on every reapply.
        # Postgres CREATE MATERIALIZED VIEW does not support OR REPLACE,
        # so IF NOT EXISTS is the only available guard.
        text = _normalized_schema()
        self.assertRegex(
            text,
            r"CREATE MATERIALIZED VIEW IF NOT EXISTS analytics_daily_rollup",
        )

    def test_view_reads_from_analytics_events(self) -> None:
        body = _rollup_view_body()
        self.assertRegex(body, r"FROM analytics_events")

    def test_view_groups_by_required_key_columns(self) -> None:
        # The key has to include `issue` because every dashboard read
        # accepts an issue filter; without it an issue-scoped read
        # would double-count. `cost_source` is in the key so the
        # cost-coverage panel can read from the rollup without
        # decomposing the `unknown-price` / `no-usage` / `reported` /
        # `estimated` cohorts after the fact.
        body = _rollup_view_body()
        for key_col in (
            "repo",
            "issue",
            "event",
            "stage",
            "backend",
            "cost_source",
        ):
            with self.subTest(column=key_col):
                # GROUP BY column listed verbatim; the day expression
                # is asserted separately because it carries a cast.
                self.assertRegex(body, rf"\b{key_col}\b")
        # `day` is derived from `ts AT TIME ZONE 'UTC'`::date -- the
        # cast normalises naive / non-UTC timestamps so the rollup is
        # consistent across writers.
        self.assertRegex(
            body,
            r"\(ts AT TIME ZONE 'UTC'\)::date\s+AS\s+day",
        )

    def test_view_exposes_required_aggregate_columns(self) -> None:
        # Every aggregate the dashboard / read model wants to read off
        # the rollup must be present. A silently-dropped aggregate
        # would force a fallback to the raw events table, which is
        # what Layer 4 exists to avoid.
        body = _rollup_view_body()
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


class AnalyticsDailyRollupAggregationTest(unittest.TestCase):
    """The `analytics_daily_rollup` materialized view is the pre-aggregated
    target the dashboard reads from instead of scanning the raw run
    tables. These tests pin the create statement's idempotency, the key
    columns, and the aggregate columns so a refactor that drops the
    timeout/failure counts or token sums (which the dashboard's
    reliability tiles and KPI strip read from) fails in the hermetic
    suite -- before an operator sees a broken dashboard.
    """

    def test_view_duration_count_uses_not_null_filter(self) -> None:
        # `duration_s_count` is the row count where duration_s is
        # populated -- not the raw row count. Without that, a consumer
        # recovering `AVG(duration_s)` as `SUM/COUNT` would divide by
        # the wrong denominator on rows where duration was NULL.
        body = _rollup_view_body()
        self.assertRegex(
            body,
            r"SUM\(CASE WHEN duration_s IS NOT NULL THEN 1 ELSE 0 END\)\s+"
            r"AS\s+duration_s_count",
        )

    def test_failed_count_requires_nonzero_exit(self) -> None:
        body = _rollup_view_body()
        # Non-zero exit_code is the failure signal; NULL exit_code
        # stays excluded so a `stage_enter` row never counts as a
        # failure.
        self.assertRegex(
            body,
            r"SUM\(CASE WHEN exit_code IS NOT NULL AND exit_code <> 0 "
            r"THEN 1 ELSE 0 END\)\s+AS\s+failed_count",
        )

    def test_timeout_count_uses_agent_exit(self) -> None:
        body = _rollup_view_body()
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
        body = _rollup_view_body()
        self.assertRegex(body, r"COUNT\(\*\)\s+AS\s+event_count")


class AnalyticsDailyRollupIndexTest(unittest.TestCase):
    """The `analytics_daily_rollup` materialized view is the pre-aggregated
    target the dashboard reads from instead of scanning the raw run
    tables. These tests pin the create statement's idempotency, the key
    columns, and the aggregate columns so a refactor that drops the
    timeout/failure counts or token sums (which the dashboard's
    reliability tiles and KPI strip read from) fails in the hermetic
    suite -- before an operator sees a broken dashboard.
    """

    def test_unique_index_treats_nulls_as_equal(self) -> None:
        # `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a unique
        # index. NULLS NOT DISTINCT (Postgres 15+) collapses NULL
        # stage / backend / cost_source values into one row -- the same
        # way GROUP BY already does -- so the index is genuinely
        # unique across the view's contents. The current sync uses the
        # non-concurrent variant, so the index is forward-compat
        # plumbing rather than load-bearing today.
        text = _normalized_schema()
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
        text = _normalized_schema()
        self.assertRegex(
            text,
            r"CREATE INDEX IF NOT EXISTS "
            r"analytics_daily_rollup_day_repo_idx\s+"
            r"ON analytics_daily_rollup\s*\(\s*day,\s*repo\s*\)",
        )
