# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live PostgreSQL analytics schema and rollup tests."""

import os


import tempfile


import unittest


from pathlib import Path


from typing import NamedTuple


from tests.analytics_sync_reload import (
    reload_sync as _reload,
    sync_for_records as _sync_for_records,
)

from tests.analytics_sync_payloads import (
    write_jsonl as _write_jsonl,
    sample_record as _sample_record,
)
from tests.analytics_assertions import assert_row_fields

_POSTGRES_INSERT_DEDUP_DURATION_S = 3.0
_POSTGRES_INSERT_DEDUP_DURATION_S_SECONDARY = 1.5
_VIEW_DERIVES_FIELDS_ISSUE = 42
_VIEW_DERIVES_FIELDS_DURATION_S = 12.5
_VIEW_DERIVES_FIELDS_INPUT_TOKENS = 300
_VIEW_DERIVES_FIELDS_OUTPUT_TOKENS = 150
_VIEW_DERIVES_FIELDS_CACHED_TOKENS = 50
_VIEW_DERIVES_FIELDS_CACHE_READ_TOKENS = 20
_VIEW_DERIVES_FIELDS_COST_USD = 0.0042
_REFRESHES_AFTER_SYNC_DURATION_S = 4.0
_REFRESHES_AFTER_SYNC_OUTPUT_TOKENS = 50
_REFRESHES_AFTER_SYNC_DURATION_S_SECONDARY = 6.0
_REFRESHES_AFTER_SYNC_INPUT_TOKENS = 200
_REFRESHES_AFTER_SYNC_OUTPUT_TOKE_SECONDARY = 80
_REFRESHES_AFTER_SYNC_COST_USD = 0.2


class _AgentRunProjection(NamedTuple):
    model: str
    total_tokens: int
    total_cache: int
    bucket: str
    failed: bool
    has_cost: bool
    cost_source: str


class _DailyRollupProjection(NamedTuple):
    total_in: int
    total_out: int
    total_cached: int
    total_cache_read: int
    total_cache_write: int
    total_cost: object
    duration_sum: float
    duration_count: int
    failed_count: int
    timed_out_count: int
    event_count: int


SAMPLE_TIMESTAMP = "2026-05-25T12:00:00+00:00"


_STAGE_ENTER = "stage_enter"


_AGENT_EXIT = "agent_exit"


_STAGE_IMPLEMENTING = "implementing"


_ISSUE_KEY = "issue"


_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"


_DB_URL_ENV = "ANALYTICS_DB_URL"


_DB_URL = "postgresql://h/db"


_TEST_DB_URL_ENV = "ANALYTICS_TEST_DB_URL"


_SYNC_MODULE = "orchestrator.analytics.sync"


_LOG_FILENAME = "a.jsonl"


_ENCODING = "utf-8"


def _sync_live_records(test_case, db_url: str, records: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / _LOG_FILENAME
        _write_jsonl(path, records)
        analytics_sync = _reload(
            {
                _LOG_PATH_ENV: str(path),
                _DB_URL_ENV: db_url,
            }
        )[1]
        test_case.assertEqual(
            analytics_sync.sync_jsonl_to_postgres().inserted,
            len(records),
        )


def _fetch_live_row(db_url: str, query: str, issue: int):
    import psycopg

    with psycopg.connect(db_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (issue,))
            return cursor.fetchone()


def _expected_rollup(records: list[dict]) -> dict[str, object]:
    return {
        "total_in": sum(record["input_tokens"] for record in records),
        "total_out": sum(record["output_tokens"] for record in records),
        "total_cached": sum(record["cached_tokens"] for record in records),
        "total_cache_read": sum(record["cache_read_tokens"] for record in records),
        "total_cache_write": sum(record["cache_write_tokens"] for record in records),
        "duration_sum": sum(record["duration_s"] for record in records),
        "duration_count": len(records),
        "failed_count": sum(record["exit_code"] != 0 for record in records),
        "timed_out_count": sum(record["timed_out"] for record in records),
        "event_count": len(records),
    }


class AnalyticsSyncLiveDdlTest(unittest.TestCase):
    """End-to-end DDL + insert against a real Postgres.

    Opt-in via `ANALYTICS_TEST_DB_URL=<libpq URL>` because most CI
    runners (and local dev shells) do not have Postgres available --
    a hermetic suite must never assume a live database. When the
    variable is set the test:

      1. Applies `analytics-db/init/01-schema.sql` against the target
         database -- the `IF NOT EXISTS` guards keep this safe to
         re-run across test invocations.
      2. Truncates `analytics_events` so the dedup assertions start
         from a known state.
      3. Runs `sync_jsonl_to_postgres` against a temp JSONL file.
      4. Asserts that the first run inserts every record and that a
         second run inserts zero -- exercising both the DDL and the
         `INSERT ... ON CONFLICT (content_hash) DO NOTHING` path the
         reviewer flagged.

    This is what makes the partial-index vs. plain-index distinction
    concrete: Postgres only accepts `ON CONFLICT (content_hash)` as
    the arbiter when the index is non-partial (or when the partial
    predicate is repeated in the conflict target). A future change
    that re-partials the index would fail the second insert here
    with `there is no unique or exclusion constraint matching the ON
    CONFLICT specification`, surfacing the regression before it ships.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_url = os.environ.get(_TEST_DB_URL_ENV, "").strip()
        if not cls.db_url:
            raise unittest.SkipTest(
                f"{_TEST_DB_URL_ENV} not set; live Postgres integration "
                "test skipped. Set it to a libpq URL pointing at the "
                "compose service (or any disposable Postgres) to run."
            )
        try:
            import psycopg
        except ImportError as exc:
            raise unittest.SkipTest(f"psycopg not available: {exc}")
        assert psycopg is not None

    def test_real_postgres_insert_and_dedup(self) -> None:
        self._apply_schema()
        records = [
            _sample_record(issue=1, event=_STAGE_ENTER, stage="ready"),
            _sample_record(issue=2, event=_AGENT_EXIT, duration_s=_POSTGRES_INSERT_DEDUP_DURATION_S),
            _sample_record(
                issue=3,
                event="stage_evaluation",
                stage="validating",
                duration_s=_POSTGRES_INSERT_DEDUP_DURATION_S_SECONDARY,
                result="ok",
            ),
        ]
        with _sync_for_records(records, db_url=self.db_url) as (
            _,
            analytics_sync,
        ):
            first = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(first.inserted, len(records))
            self.assertEqual(first.skipped_duplicate, 0)
            self.assertEqual(self._row_count(), len(records))

            second = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, len(records))
            self.assertEqual(self._row_count(), len(records))

    def test_analytics_agent_runs_view_derives_fields(self) -> None:
        # Apply the DDL, insert one `agent_exit` row carrying the
        # fields the view derives over, and assert the derivations
        # compute as advertised. This is the live-DB counterpart to
        # the text-based checks in `tests/test_analytics_schema.py`:
        # a typo in the view body would compile-fail here even if the
        # text regex still matched.
        self._apply_schema()
        agent_run = _sample_record(
            issue=_VIEW_DERIVES_FIELDS_ISSUE,
            event=_AGENT_EXIT,
            stage=_STAGE_IMPLEMENTING,
            agent_role="developer",
            backend="codex",
            review_round=4,
            retry_count=1,
            duration_s=_VIEW_DERIVES_FIELDS_DURATION_S,
            exit_code=0,
            timed_out=False,
            input_tokens=_VIEW_DERIVES_FIELDS_INPUT_TOKENS,
            output_tokens=_VIEW_DERIVES_FIELDS_OUTPUT_TOKENS,
            cached_tokens=_VIEW_DERIVES_FIELDS_CACHED_TOKENS,
            cache_read_tokens=_VIEW_DERIVES_FIELDS_CACHE_READ_TOKENS,
            cache_write_tokens=10,
            models=["gpt-5-codex"],
            cost_usd=_VIEW_DERIVES_FIELDS_COST_USD,
            cost_source="estimated",
        )
        _sync_live_records(self, self.db_url, [agent_run])
        row = _fetch_live_row(
            self.db_url,
            "SELECT model, total_tokens, total_cache_tokens, "
            "review_round_bucket, failed, has_cost, cost_source "
            "FROM analytics_agent_runs WHERE issue = %s",
            agent_run[_ISSUE_KEY],
        )
        self.assertIsNotNone(row)
        projection = _AgentRunProjection(*row)
        assert_row_fields(
            self,
            projection,
            {
                "model": agent_run["models"][0],
                "total_tokens": agent_run["input_tokens"] + agent_run["output_tokens"],
                "total_cache": (
                    agent_run["cached_tokens"]
                    + agent_run["cache_read_tokens"]
                    + agent_run["cache_write_tokens"]
                ),
                "bucket": "3-5",
                "failed": False,
                "has_cost": True,
                "cost_source": "estimated",
            },
        )

    def test_daily_rollup_refreshes_after_sync(self) -> None:
        # End-to-end Layer 4: insert two `agent_exit` rows on the same
        # UTC day with matching key columns, run the sync (which triggers
        # the post-commit `REFRESH MATERIALIZED VIEW`), and assert the
        # rollup row carries the summed token / cost / duration columns
        # and the failure / timeout counts the dashboard's reliability
        # tiles read off. A column typo or a wrong CASE predicate would
        # compile-fail here even if the text regexes in
        # `tests/test_analytics_schema.py` still matched.
        self._apply_schema()
        successful_run = _sample_record(
            issue=7,
            event=_AGENT_EXIT,
            stage=_STAGE_IMPLEMENTING,
            backend="claude",
            cost_source="reported",
            duration_s=_REFRESHES_AFTER_SYNC_DURATION_S,
            exit_code=0,
            timed_out=False,
            input_tokens=100,
            output_tokens=_REFRESHES_AFTER_SYNC_OUTPUT_TOKENS,
            cached_tokens=5,
            cache_read_tokens=3,
            cache_write_tokens=2,
            cost_usd=0.1,
        )
        failed_run = _sample_record(
            issue=successful_run[_ISSUE_KEY],
            event=_AGENT_EXIT,
            stage=_STAGE_IMPLEMENTING,
            backend="claude",
            cost_source="reported",
            ts="2026-05-25T13:30:00+00:00",
            duration_s=_REFRESHES_AFTER_SYNC_DURATION_S_SECONDARY,
            exit_code=1,
            timed_out=True,
            input_tokens=_REFRESHES_AFTER_SYNC_INPUT_TOKENS,
            output_tokens=_REFRESHES_AFTER_SYNC_OUTPUT_TOKE_SECONDARY,
            cached_tokens=10,
            cache_read_tokens=4,
            cache_write_tokens=1,
            cost_usd=_REFRESHES_AFTER_SYNC_COST_USD,
        )
        runs = [successful_run, failed_run]
        _sync_live_records(self, self.db_url, runs)
        row = _fetch_live_row(
            self.db_url,
            "SELECT total_input_tokens, total_output_tokens, "
            "total_cached_tokens, total_cache_read_tokens, "
            "total_cache_write_tokens, total_cost_usd, "
            "duration_s_sum, duration_s_count, "
            "failed_count, timed_out_count, event_count "
            "FROM analytics_daily_rollup WHERE issue = %s",
            successful_run[_ISSUE_KEY],
        )
        self.assertIsNotNone(row)
        projection = _DailyRollupProjection(*row)
        assert_row_fields(self, projection, _expected_rollup(runs))
        # Numeric comparison: the schema uses NUMERIC(20, 10), so the
        # sum may come back as a Decimal. Cast both sides to float for
        # the comparison so an exact-decimal mismatch on the literal
        # does not blow up the assertion.
        self.assertAlmostEqual(
            float(projection.total_cost),
            sum(run["cost_usd"] for run in runs),
            places=6,
        )

    def _apply_schema(self) -> None:
        import psycopg

        repo_root = Path(__file__).resolve().parent.parent
        schema_path = repo_root / "analytics-db" / "init" / "01-schema.sql"
        with psycopg.connect(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_path.read_text(encoding=_ENCODING))
                cur.execute("TRUNCATE analytics_events RESTART IDENTITY")
            conn.commit()

    def _row_count(self) -> int:
        import psycopg

        with psycopg.connect(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM analytics_events")
                row = cur.fetchone()
        return int(row[0]) if row else 0
