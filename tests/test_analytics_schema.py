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


if __name__ == "__main__":
    unittest.main()
