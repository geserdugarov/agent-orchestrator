# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import importlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

from orchestrator.analytics import _sync_rows


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
SAMPLE_NAIVE_TIMESTAMP = "2026-05-25T12:00:00"
TEST_BATCH_SIZE = 3
PARTIAL_BATCH_RECORD_COUNT = TEST_BATCH_SIZE + 2
CLI_CLOCK_TOLERANCE_SECONDS = 5

# A SHA-256 content hash is 64 hex chars; the promoted-column tests
# assert that width on the trailing hash param.
_CONTENT_HASH_HEX_LEN = 64
# A batch cap comfortably larger than any test's record count, so the
# whole file flushes as one trailing partial batch.
_LARGE_BATCH_SIZE = 500

# Analytics event names asserted as JSONL `event` values, plus the one
# pipeline stage carried on `stage` records that recurs across tests.
_STAGE_ENTER = "stage_enter"
_AGENT_EXIT = "agent_exit"
_STAGE_IMPLEMENTING = "implementing"

# JSONL field key the record fixtures and live-DB lookups share.
_ISSUE_KEY = "issue"

# Config env keys the sync re-parses at reload time, plus the "disabled"
# sentinel and a throwaway libpq URL for the enabled-sink tests.
# Credential-bearing URLs stay inline where the redaction assertions
# need their exact shape.
_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"
_DB_URL_ENV = "ANALYTICS_DB_URL"
_SENTINEL_DISABLED = "off"
_DB_URL = "postgresql://h/db"

# Opt-in libpq URL for the live-Postgres integration test; unset skips it.
_TEST_DB_URL_ENV = "ANALYTICS_TEST_DB_URL"

# The module under test is also the logger name its records land under,
# so both the reload and `assertLogs` key off the same string. The
# rollup refresh statement and the CLI patch targets recur enough to
# name.
_SYNC_MODULE = "orchestrator.analytics.sync"
_LOG_LEVEL_INFO = "INFO"
_REFRESH_STMT = "REFRESH MATERIALIZED VIEW"
_BATCH_SIZE_ATTR = "_BATCH_SIZE"
_STDOUT = "sys.stdout"

# JSONL fixtures land in this file inside each test's temp dir.
_LOG_FILENAME = "a.jsonl"
_ENCODING = "utf-8"

# One recorded `executemany` batch: the SQL text plus its params list.
_Batch = tuple[str, list[tuple]]


def _passthrough(json_obj):
    """Identity `json_adapter`: the fake connection stores Python
    objects verbatim, so the sync needs no psycopg JSON wrapping."""
    return json_obj


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `orchestrator.config`, `orchestrator.analytics`, and
    `orchestrator.analytics.sync` against the given hermetic env.

    The analytics package owns its own `ANALYTICS_LOG_PATH` /
    `ANALYTICS_RETENTION_DAYS` / `ANALYTICS_DB_URL` parsing, and
    `analytics.sync` reads both `ANALYTICS_LOG_PATH` and
    `ANALYTICS_DB_URL` off the parent package at call time, so the
    parent must be popped alongside `sync` for the test env to land.
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics", None)
        sys.modules.pop(_SYNC_MODULE, None)
        # `import_module` re-executes each freshly-popped module against
        # the patched env; a `from orchestrator import analytics` would
        # instead hand back the parent package's stale cached attribute.
        analytics = importlib.import_module("orchestrator.analytics")
        analytics_sync = importlib.import_module(_SYNC_MODULE)
        return analytics, analytics_sync


class _FakeCursor:
    """Records every `executemany` batch and emulates ON CONFLICT.

    Implemented as a context manager so the production `with
    conn.cursor() as cur:` block works unchanged. The production sync
    issues one `cur.execute("SELECT content_hash ...")` at startup
    (the dedup pre-check) and then accumulates validated row tuples
    flushed per batch via `cur.executemany`. For the SELECT shape the
    fake snapshots the connection's `pre_check_hashes` (falling back
    to `seen_hashes` when the test has not overridden it, so the
    common "DB state == executemany ON CONFLICT view" case keeps
    working) into a per-execute result list and exposes the rows
    through `__iter__`, matching psycopg3's tuple-per-row iteration.
    For the executemany shape the fake fans params_seq out into the
    flattened `inserts` / `duplicate_calls` recorders so per-row
    assertions keep working, and records the raw (sql, params_list)
    pair in `batches` so tests can assert on batch shape. `rowcount`
    mirrors psycopg's per-`executemany` total: the count of rows
    that actually landed (a conflict skip contributes 0).
    """

    def __init__(self, store: "_FakeConnection") -> None:
        self._store = store
        self.rowcount = 0
        self._select_rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Never suppress the exception: sync errors must propagate."""

    def execute(self, sql: str, sql_params=None) -> None:
        self._store.select_calls.append((sql, sql_params))
        # Refresh of `analytics_daily_rollup` rides on `cur.execute`
        # (not `executemany`), so a separate raise flag lets tests
        # exercise the swallow-and-log path without affecting the
        # batched insert path that `raise_on_executemany` covers.
        if (
            self._store.raise_on_refresh is not None
            and _REFRESH_STMT in sql.upper()
        ):
            raise self._store.raise_on_refresh
        if sql.lstrip().upper().startswith("SELECT"):
            source = (
                self._store.seen_hashes
                if self._store.pre_check_hashes is None
                else self._store.pre_check_hashes
            )
            self._select_rows = [
                (content_hash,) for content_hash in sorted(source)
            ]
        else:
            self._select_rows = []
        self.rowcount = len(self._select_rows)

    def __iter__(self):
        return iter(self._select_rows)

    def executemany(self, sql: str, params_seq) -> None:
        # A configured driver-failure flag raises before any batch is
        # recorded, so the transaction test sees the rollback path with
        # no partial batch persisted.
        if self._store.raise_on_executemany is not None:
            raise self._store.raise_on_executemany
        # Materialize once so a generator caller can't double-spend
        # the iterator between the recorder and the rowcount math.
        params_list = list(params_seq)
        self._store.batches.append((sql, params_list))
        inserted_in_batch = 0
        for row_params in params_list:
            # Hash is the last param; relies on the column order
            # baked into `_build_insert_sql`. If the schema's column
            # order ever changes the test will fail loudly here --
            # which is fine, the test would be wrong in lock-step
            # with the production code.
            content_hash = row_params[-1]
            if content_hash in self._store.seen_hashes:
                self._store.duplicate_calls.append((sql, row_params))
            else:
                self._store.seen_hashes.add(content_hash)
                self._store.inserts.append((sql, row_params))
                inserted_in_batch += 1
        self.rowcount = inserted_in_batch


class _FakeConnection:
    """In-memory stand-in for a psycopg connection.

    Captures inserts and conflict-skips, the per-batch `executemany`
    calls, plus commit / rollback / close so tests can assert that
    the sync commits on success and rolls back on error. The
    `seen_hashes` set models the database's ON CONFLICT view (what
    executemany treats as already-present), and `pre_check_hashes`
    overrides what the startup SELECT returns -- defaulting to `None`
    so the SELECT snapshots `seen_hashes` and mirrors reality, but
    lettable to a separate set in race-safe-backstop tests where the
    pre-check stale view must diverge from the DB.
    """

    def __init__(self) -> None:
        self.inserts: list[tuple[str, tuple]] = []
        self.duplicate_calls: list[tuple[str, tuple]] = []
        self.batches: list[_Batch] = []
        self.select_calls: list[tuple[str, object]] = []
        self.seen_hashes: set[str] = set()
        self.pre_check_hashes: set[str] | None = None
        self.commit_called = 0
        self.rollback_called = 0
        self.close_called = 0
        self.raise_on_executemany: Exception | None = None
        self.raise_on_refresh: Exception | None = None

    def cursor(self) -> _FakeCursor:
        # `_FakeCursor.executemany` consults `raise_on_executemany`
        # itself, so every call gets the same plain cursor.
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_called += 1

    def rollback(self) -> None:
        self.rollback_called += 1

    def close(self) -> None:
        self.close_called += 1

    def as_connect(self, _url: str) -> "_FakeConnection":
        """Serve as the sync's `connect(db_url) -> conn` callable,
        always yielding this same fake so a test can inspect the
        executed SQL / commits after the sync returns."""
        return self


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Mirror `analytics.append_record`'s on-disk encoding so the
    content hash the sync computes matches what a real writer would
    produce.
    """
    _write_raw_lines(
        path, [json.dumps(record, sort_keys=True) for record in records]
    )


def _write_raw_lines(path: Path, lines: list[str]) -> None:
    """Write pre-rendered JSONL rows (or deliberate garbage / blanks)
    verbatim, one per line, so the malformed-input tests can mix good
    records with non-JSON without repeating the open / encode dance.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=_ENCODING) as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")


def _sample_record(
    *,
    issue: int = 1,
    event: str = _STAGE_ENTER,
    ts: str = SAMPLE_TIMESTAMP,
    **extras,
) -> dict:
    record = {
        "ts": ts,
        "repo": "owner/repo",
        _ISSUE_KEY: issue,
        "event": event,
    }
    record.update(extras)
    return record


def _sample_records(count: int) -> list[dict]:
    return [_sample_record(issue=issue) for issue in range(1, count + 1)]


def _record_line(**kwargs) -> str:
    """Canonical single-line JSONL encoding of a sample record, for the
    malformed-input tests that interleave a good record with raw garbage
    lines."""
    return json.dumps(_sample_record(**kwargs), sort_keys=True)


@contextlib.contextmanager
def _reloaded_sync(
    write_log, *, db_url: str = _DB_URL, filename: str = _LOG_FILENAME,
):
    """Materialize a JSONL log under a temp dir via `write_log(path)`,
    reload the sync module bound to that path + `db_url`, and yield
    `(path, analytics_sync)`. Folds the temp-file / reload boilerplate
    the insert-path tests otherwise repeat verbatim.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / filename
        write_log(path)
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: str(path),
            _DB_URL_ENV: db_url,
        })
        yield path, analytics_sync


def _sync_for_records(records: list[dict], **kwargs):
    """`_reloaded_sync` seeded from well-formed record dicts."""
    return _reloaded_sync(lambda path: _write_jsonl(path, records), **kwargs)


def _sync_for_lines(lines: list[str], **kwargs):
    """`_reloaded_sync` seeded from raw JSONL / garbage lines."""
    return _reloaded_sync(lambda path: _write_raw_lines(path, lines), **kwargs)


def _run_sync(analytics_sync, connection, **kwargs):
    """Drive the sync through a fake connection with the identity JSON
    adapter every insert-path test shares."""
    return analytics_sync.sync_jsonl_to_postgres(
        connect=connection.as_connect,
        json_adapter=_passthrough,
        **kwargs,
    )


def _sync_capturing_logs(test_case, analytics_sync, connection, **kwargs):
    """Run the sync through `connection`, assert it emits at least one
    INFO record, and return `(sync_result, log_lines)` so callers read
    the captured output off a plain list rather than the `assertLogs`
    control variable.
    """
    with contextlib.ExitStack() as cleanup:
        captured = cleanup.enter_context(
            test_case.assertLogs(_SYNC_MODULE, level=_LOG_LEVEL_INFO),
        )
        sync_result = _run_sync(analytics_sync, connection, **kwargs)
    return sync_result, list(captured.output)


def _reset_root_logger() -> None:
    """Drop every handler off the root logger so a UTC `StreamHandler`
    a CLI test installs never leaks into a sibling test in the same
    process."""
    root = logging.getLogger()
    for stale_handler in list(root.handlers):
        root.removeHandler(stale_handler)


def _select_sqls(connection) -> list[str]:
    """SQL text of every startup SELECT the fake recorded, dropping the
    post-commit REFRESH so a pre-check tally never overcounts it."""
    return [
        sql for sql, _ in connection.select_calls
        if sql.lstrip().upper().startswith("SELECT")
    ]


def _refresh_sqls(connection) -> list[str]:
    """Every `REFRESH MATERIALIZED VIEW` statement the fake recorded."""
    return [
        sql for sql, _ in connection.select_calls if _REFRESH_STMT in sql
    ]


class AnalyticsDbUrlConfigTest(unittest.TestCase):
    """`ANALYTICS_DB_URL` parses at import inside the analytics
    package: empty / sentinel disables; a real URL passes through
    verbatim so a libpq URL is the single-knob endpoint contract.
    """

    def test_default_is_disabled(self) -> None:
        analytics, _ = _reload()
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_empty_string_disables(self) -> None:
        analytics, _ = _reload({_DB_URL_ENV: ""})
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_sentinel_values_disable(self) -> None:
        for sentinel in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(value=sentinel):
                analytics, _ = _reload({_DB_URL_ENV: sentinel})
                self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_real_url_passes_through(self) -> None:
        url = "postgresql://u:p@db.example.com:5432/orchestrator_analytics"
        analytics, _ = _reload({_DB_URL_ENV: url})
        self.assertEqual(analytics.ANALYTICS_DB_URL, url)

    def test_whitespace_stripped(self) -> None:
        analytics, _ = _reload(
            {_DB_URL_ENV: "  postgresql://h/db  "}
        )
        self.assertEqual(analytics.ANALYTICS_DB_URL, "postgresql://h/db")


class AnalyticsSyncDisabledTest(unittest.TestCase):
    """When either env knob is unset the sync is a silent no-op: no
    connection attempt, no row insertion, no error. Mirrors how
    `analytics.append_record` no-ops when the sink is disabled.
    """

    def test_no_op_when_db_url_unset(self) -> None:
        records = [_sample_record()]
        with _sync_for_records(records, db_url="") as (_, analytics_sync):
            connected = []
            sync_result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(sync_result.inserted, 0)
            self.assertEqual(sync_result.total_lines, 0)

    def test_no_op_when_log_path_unset(self) -> None:
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: _DB_URL,
        })
        connected = []
        sync_result = analytics_sync.sync_jsonl_to_postgres(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(sync_result.inserted, 0)

    def test_no_op_when_log_file_missing(self) -> None:
        # Configured but file not created yet (orchestrator hasn't
        # emitted any record). Don't connect, don't fail. The no-op
        # writer leaves the path absent so the sync sees a missing file.
        with _reloaded_sync(lambda path: None) as (_, analytics_sync):
            connected = []
            sync_result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(sync_result.inserted, 0)


class AnalyticsSyncInsertTest(unittest.TestCase):
    """Happy-path inserts: each well-formed JSONL line becomes one
    INSERT carrying the promoted columns + extras + content_hash; the
    transaction commits on success.
    """

    def test_inserts_each_record_once(self) -> None:
        records = [
            _sample_record(issue=1, event=_STAGE_ENTER, stage=_STAGE_IMPLEMENTING),
            _sample_record(issue=2, event=_AGENT_EXIT, duration_s=12.5),
        ]
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, len(records))
            self.assertEqual(sync_result.skipped_duplicate, 0)
            self.assertEqual(sync_result.skipped_malformed, 0)
            self.assertEqual(sync_result.total_lines, len(records))
            self.assertEqual(len(fake.inserts), len(records))
            # Two commits: one for the events insert, one after the
            # post-commit refresh of `analytics_daily_rollup`.
            self.assertEqual(fake.commit_called, 2)
            self.assertEqual(fake.rollback_called, 0)
            self.assertEqual(fake.close_called, 1)

    def test_promoted_columns_and_extras_split(self) -> None:
        record = _sample_record(
            event=_AGENT_EXIT,
            stage=_STAGE_IMPLEMENTING,
            duration_s=42.0,
            backend="claude",
            session_id="sess-abc",
            input_tokens=100,
            custom_future_key="something-new",
        )
        with _sync_for_records([record]) as (path, analytics_sync):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
            _, row_values = fake.inserts[0]
            promoted = _sync_rows._PROMOTED_COLUMNS
            for column in (
                "repo",
                "issue",
                "event",
                "stage",
                "backend",
                "session_id",
                "input_tokens",
            ):
                self.assertEqual(row_values[promoted.index(column)], record[column])
            # Extras column lives after the promoted block.
            extras_idx = len(promoted)
            self.assertEqual(
                row_values[extras_idx], {"custom_future_key": "something-new"}
            )
            # source_path / source_line / content_hash trail it.
            self.assertEqual(row_values[extras_idx + 1], str(path))
            self.assertEqual(row_values[extras_idx + 2], 1)
            # Content hash matches the canonical encoding of the source
            # record, not the unsorted one we passed in -- this is
            # what makes dedup robust against prune-induced rewrites.
            self.assertIsInstance(row_values[extras_idx + 3], str)
            self.assertEqual(len(row_values[extras_idx + 3]), _CONTENT_HASH_HEX_LEN)

    def test_ts_parsed_to_datetime(self) -> None:
        # The ts column is TIMESTAMPTZ; psycopg expects a datetime,
        # not a string. A naive string would be silently inserted as
        # text in some configurations.
        with _sync_for_records([_sample_record(ts=SAMPLE_TIMESTAMP)]) as (
            _, analytics_sync,
        ):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
            _, row_values = fake.inserts[0]
            ts_value = row_values[_sync_rows._PROMOTED_COLUMNS.index("ts")]
            self.assertIsInstance(ts_value, datetime)
            self.assertIsNotNone(ts_value.tzinfo)


class AnalyticsSyncDedupTest(unittest.TestCase):
    """Repeated runs over the same file insert each record exactly
    once. This is the core idempotency guarantee the issue calls
    out.
    """

    def test_second_run_inserts_nothing(self) -> None:
        records = _sample_records(2)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            first = _run_sync(analytics_sync, fake)
            second = _run_sync(analytics_sync, fake)
            self.assertEqual(first.inserted, len(records))
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, len(records))
            # Only the 2 originals are durably persisted.
            self.assertEqual(len(fake.inserts), len(records))

    def test_post_prune_renumbering_stays_unique(self) -> None:
        # The realistic post-prune scenario: file had 3 records, the
        # prune dropped record #1, leaving #2 + #3 at line numbers 1
        # and 2. A naive (source_path, source_line) key would
        # re-insert them under the freed (path, 1) / (path, 2) keys.
        # Content-hash dedup keeps them out.
        original_records = [
            _sample_record(issue=1, event="a"),
            _sample_record(issue=2, event="b"),
            _sample_record(issue=3, event="c"),
        ]
        with _sync_for_records(original_records) as (path, analytics_sync):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
            # Operator runs prune; file now has only #2 + #3 at lines 1 + 2.
            _write_jsonl(path, original_records[1:])
            second = _run_sync(analytics_sync, fake)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, len(original_records[1:]))


class AnalyticsSyncMalformedTest(unittest.TestCase):
    """Malformed lines mirror the prune helper's tolerance: blanks are
    silently skipped, garbage / missing keys are counted and logged
    but never abort the sync. The JSONL file is never rewritten.
    """

    def test_blank_lines_are_silent(self) -> None:
        lines = ["", _record_line(), "   "]
        with _sync_for_lines(lines) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(sync_result.skipped_malformed, 0)
            self.assertEqual(sync_result.total_lines, 3)

    def test_non_json_line_counted_and_skipped(self) -> None:
        lines = ["this is not json", _record_line()]
        with _sync_for_lines(lines) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(sync_result.skipped_malformed, 1)
            self.assertEqual(sync_result.malformed_line_numbers, (1,))
            # The good record on line 2 still gets inserted -- one bad
            # line cannot poison the whole sync.
            self.assertEqual(len(fake.inserts), 1)

    def test_json_non_object_skipped(self) -> None:
        # `null`, lists, numbers parse cleanly but aren't dict
        # records; treat them as malformed rather than crashing.
        lines = ["null", "[1, 2, 3]", "42", _record_line()]
        with _sync_for_lines(lines) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(sync_result.skipped_malformed, 3)

    def test_missing_required_key_skipped(self) -> None:
        # Records missing `ts` / `repo` / `issue` / `event` cannot be
        # inserted (NOT NULL columns) so the sync filters them out
        # rather than letting psycopg raise mid-transaction.
        lines = [
            '{"repo": "o/r", "issue": 1, "event": "x"}',  # missing ts
            '{"ts": "2026-05-25T12:00:00+00:00", "issue": 1, "event": "x"}',  # missing repo
            '{"ts": "2026-05-25T12:00:00+00:00", "repo": "o/r", "event": "x"}',  # missing issue
            '{"ts": "2026-05-25T12:00:00+00:00", "repo": "o/r", "issue": 1}',  # missing event
            _record_line(),
        ]
        with _sync_for_lines(lines) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(sync_result.skipped_malformed, 4)

    def test_unparseable_ts_skipped(self) -> None:
        # Parallel to `prune_old_records`'s behavior on a garbled `ts`:
        # the record is preserved verbatim in the JSONL file (sync is
        # read-only) but is not inserted.
        with _sync_for_lines([
            '{"ts": "not-a-date", "repo": "o/r", "issue": 1, "event": "x"}',
            _record_line(),
        ]) as (path, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(sync_result.skipped_malformed, 1)
            # File untouched -- the sync never rewrites; operator
            # cleanup is the same as for `prune_old_records`.
            preserved = path.read_text(encoding=_ENCODING).splitlines()
            self.assertEqual(len(preserved), 2)

    def test_naive_ts_treated_as_utc(self) -> None:
        # Same forward-compat as `prune_old_records`: records written
        # by an older writer without tz info are interpreted as UTC
        # rather than being rejected as malformed.
        with _sync_for_records(
            [_sample_record(ts=SAMPLE_NAIVE_TIMESTAMP)],
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            _, row_values = fake.inserts[0]
            ts_value = row_values[_sync_rows._PROMOTED_COLUMNS.index("ts")]
            self.assertEqual(ts_value.tzinfo, timezone.utc)


class AnalyticsSyncTransactionTest(unittest.TestCase):
    """A driver-side error mid-stream rolls the transaction back so
    a partial batch is never committed. The exception propagates so
    the CLI surfaces a non-zero exit code rather than reporting
    "success" on a half-inserted batch.
    """

    def test_execute_error_rolls_back_and_propagates(self) -> None:
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.raise_on_executemany = RuntimeError(
                "simulated driver failure"
            )
            with self.assertRaises(RuntimeError):
                _run_sync(analytics_sync, fake)
            self.assertEqual(fake.commit_called, 0)
            self.assertEqual(fake.rollback_called, 1)
            self.assertEqual(fake.close_called, 1)


class _NegativeRowcountCursor:
    """Cursor stand-in whose driver strips the per-`executemany`
    rowcount (reports -1), forcing `_flush_batch` onto its
    whole-batch-inserted fallback."""

    rowcount = -1

    def __init__(self) -> None:
        self.calls: list[list[tuple]] = []

    def executemany(self, sql: str, params_seq) -> None:
        # Materialize so a generator caller is not double-spent.
        self.calls.append(list(params_seq))


class _RejectingBatchCursor:
    """Cursor stand-in that fails if `executemany` runs at all, pinning
    the empty-batch no-op contract."""

    rowcount = 0

    def executemany(self, sql: str, params_seq) -> None:
        raise AssertionError("executemany must not run on empty batch")


class FlushBatchRowcountTest(unittest.TestCase):
    """`_flush_batch` derives inserted-vs-duplicate from the cursor's
    per-`executemany` rowcount. A driver that strips the count entirely
    (reports -1) falls back to counting the whole batch as inserted, so
    `inserted` stays a lower bound rather than the count going negative.
    """

    def test_negative_rowcount_marks_batch_inserted(self) -> None:
        _, analytics_sync = _reload()
        cur = _NegativeRowcountCursor()
        counters = analytics_sync._SyncCounters()
        batch = [("a",), ("b",), ("c",)]
        analytics_sync._flush_batch(
            cur, "INSERT ...", batch, counters, start=0.0,
        )
        self.assertEqual(counters.inserted, 3)
        self.assertEqual(counters.skipped_duplicate, 0)
        # The buffer is cleared so the caller can refill it, and the whole
        # batch reached the wire in a single `executemany`.
        self.assertEqual(batch, [])
        self.assertEqual(len(cur.calls), 1)
        self.assertEqual(len(cur.calls[0]), 3)

    def test_empty_batch_is_a_noop(self) -> None:
        _, analytics_sync = _reload()
        counters = analytics_sync._SyncCounters()
        analytics_sync._flush_batch(
            _RejectingBatchCursor(), "INSERT ...", [], counters, start=0.0,
        )
        self.assertEqual(counters.inserted, 0)
        self.assertEqual(counters.skipped_duplicate, 0)


class AnalyticsSyncCliTest(unittest.TestCase):
    """The CLI prints a one-line summary on success and exits 1 on
    failure so a cron / systemd unit can surface the error.
    """

    def test_cli_no_op_prints_zeros(self) -> None:
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: "",
        })
        buf = io.StringIO()
        with patch(_STDOUT, buf):
            rc = analytics_sync.main([])
        self.assertEqual(rc, 0)
        self.assertIn("inserted=0", buf.getvalue())
        self.assertIn("duplicate=0", buf.getvalue())

    def test_cli_overrides_take_effect(self) -> None:
        # `--log-path` / `--db-url` should override the configured
        # values for one-off replays of archived logs.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rotated.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: "",
            })

            sync_mock = MagicMock(
                return_value=analytics_sync.SyncResult(inserted=1, total_lines=1),
            )
            with patch.object(
                analytics_sync, "sync_jsonl_to_postgres", sync_mock,
            ):
                buf = io.StringIO()
                with patch(_STDOUT, buf):
                    rc = analytics_sync.main([
                        "--log-path", str(path),
                        "--db-url", "postgresql://override/db",
                    ])
            self.assertEqual(rc, 0)
            self.assertIn("inserted=1", buf.getvalue())
            sync_mock.assert_called_once()
            self.assertEqual(sync_mock.call_args.kwargs["log_path"], path)
            self.assertEqual(
                sync_mock.call_args.kwargs["db_url"],
                "postgresql://override/db",
            )

    def test_cli_surfaces_failure_as_nonzero(self) -> None:
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: "",
        })
        with patch.object(
            analytics_sync,
            "sync_jsonl_to_postgres",
            side_effect=RuntimeError("boom"),
        ):
            buf = io.StringIO()
            with patch(_STDOUT, buf):
                rc = analytics_sync.main([])
        self.assertEqual(rc, 1)

    def test_cli_logs_and_stdout_share_utc_clock(self) -> None:
        # Regression for the reviewer's TZ-skew finding: log lines used
        # to print in local time while the stdout summary printed UTC,
        # so on a TZ+7 host the two surfaces were 7 hours apart for the
        # same event. With both pinned to UTC + an explicit "UTC"
        # marker, mixing stdout/stderr stays a coherent time stream.
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: "",
        })
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        # Restore the root logger at teardown so the UTC handler
        # `_configure_cli_logging` installs never leaks into a sibling.
        self.addCleanup(_reset_root_logger)
        # Patch BEFORE main() so the StreamHandler that
        # `_configure_cli_logging` constructs captures the patched
        # stderr (StreamHandler() resolves `sys.stderr` at __init__).
        with patch("sys.stderr", err_buf), patch(_STDOUT, out_buf):
            rc = analytics_sync.main([])
        self.assertEqual(rc, 0)
        out_text = out_buf.getvalue()
        err_text = err_buf.getvalue()
        # Both surfaces must carry the explicit "UTC" marker so a
        # mixed-stream consumer (a piped `2>&1`) can tell the
        # timestamps share a timezone.
        self.assertIn(" UTC ", out_text)
        self.assertIn(" UTC ", err_text)
        # Extract one timestamp from each surface and confirm they
        # match within a few seconds. If the log had defaulted to
        # local time (the reviewer's TZ+7 bug), the delta would be
        # measured in hours.
        ts_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
        out_match = ts_re.search(out_text)
        err_match = ts_re.search(err_text)
        self.assertIsNotNone(out_match)
        self.assertIsNotNone(err_match)
        out_ts = datetime.strptime(out_match.group(1), "%Y-%m-%d %H:%M:%S")
        err_ts = datetime.strptime(err_match.group(1), "%Y-%m-%d %H:%M:%S")
        delta = abs((out_ts - err_ts).total_seconds())
        self.assertLess(
            delta, CLI_CLOCK_TOLERANCE_SECONDS,
            f"stdout and stderr timestamps disagree by {delta}s: "
            f"out={out_match.group(1)} err={err_match.group(1)}",
        )
        # Cross-check against `now()` to confirm the shared clock is
        # actually UTC, not just any single tz. A local-time formatter
        # would land outside this window on a TZ-skewed host.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(
            abs((out_ts - now_utc).total_seconds()),
            CLI_CLOCK_TOLERANCE_SECONDS,
            "stdout summary timestamp is not UTC",
        )
        self.assertLess(
            abs((err_ts - now_utc).total_seconds()),
            CLI_CLOCK_TOLERANCE_SECONDS,
            "log timestamp is not UTC",
        )

    def test_stdout_has_timestamp_and_duration(self) -> None:
        # Operators run the sync from a terminal and expect a timestamped,
        # one-line summary with the elapsed wall-clock so a multi-thousand
        # record replay surfaces its cost without grepping the log lines.
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: "",
        })
        buf = io.StringIO()
        with patch(_STDOUT, buf):
            rc = analytics_sync.main([])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # The leading `YYYY-MM-DD HH:MM:SS UTC` timestamp gives an
        # operator mixing stdout + stderr the same wall-clock anchor
        # the log formatter prepends; the explicit "UTC" marker is
        # what makes the two streams comparable on a TZ-skewed host.
        # A missing timestamp -- or a missing tz marker -- is a
        # regression.
        self.assertRegex(
            out,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC analytics_sync:",
        )
        self.assertIn("duration_s=", out)


class AnalyticsSyncConnectionLogTest(unittest.TestCase):
    """A successful connect is logged with a redacted URL so an operator
    sees the sync actually reached the database, and credentials never
    land in the operator's log.
    """

    def test_connect_emits_connected_log(self) -> None:
        with _sync_for_records(
            [_sample_record()], db_url="postgresql://u:secret@h:5432/db",
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        joined = "\n".join(log_lines)
        self.assertIn("connecting to", joined)
        self.assertIn("connection established", joined)
        # The credential half of the URL must never appear; the redacted
        # form keeps the scheme + host + db so the operator can still
        # confirm which endpoint they hit.
        self.assertNotIn("secret", joined)
        self.assertNotIn("u:secret", joined)
        self.assertIn("***@h:5432", joined)

    def test_no_credentials_url_passes_through(self) -> None:
        _, analytics_sync = _reload()
        self.assertEqual(
            analytics_sync._redact_db_url("postgresql://h:5432/db"),
            "postgresql://h:5432/db",
        )

    def test_redact_db_url_strips_user_only(self) -> None:
        _, analytics_sync = _reload()
        self.assertIn(
            "***@h",
            analytics_sync._redact_db_url("postgresql://user@h/db"),
        )

    def test_password_query_param_is_redacted(self) -> None:
        # libpq accepts `postgresql://h/db?user=u&password=secret` --
        # netloc-only redaction would leak the password into the
        # operator's stdout. Both forms must collapse to ***.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url(
            "postgresql://h/db?user=u&password=secret&sslmode=require"
        )
        self.assertNotIn("secret", redacted)
        self.assertNotIn("user=u", redacted)
        # Non-credential params survive verbatim so the redacted URL
        # still tells the operator which SSL mode was configured.
        self.assertIn("sslmode=require", redacted)
        self.assertIn("password=", redacted)
        self.assertIn("***", redacted)

    def test_sslpassword_query_param_is_redacted(self) -> None:
        # `sslpassword` decrypts the SSL client key; same threat model
        # as `password` itself.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url(
            "postgresql://h/db?sslpassword=ssl-secret"
        )
        self.assertNotIn("ssl-secret", redacted)
        self.assertIn("sslpassword=", redacted)

    def test_query_params_are_case_insensitive(self) -> None:
        # libpq treats parameter names as case-insensitive; uppercase
        # spellings must redact identically so a `?PASSWORD=secret`
        # URL does not slip past the filter.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url(
            "postgresql://h/db?PASSWORD=secret"
        )
        self.assertNotIn("secret", redacted)

    def test_connect_log_redacts_query_password(self) -> None:
        # End-to-end regression: a query-string-password URL must not
        # leak the password into the connection log.
        with _sync_for_records(
            [_sample_record()],
            db_url="postgresql://h:5432/db?user=u&password=qs-secret",
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        joined = "\n".join(log_lines)
        self.assertNotIn("qs-secret", joined)
        self.assertIn("connection established", joined)


class AnalyticsSyncBatchTest(unittest.TestCase):
    """Batched flush semantics: validated rows accumulate into a
    `_BATCH_SIZE`-sized buffer, every full batch is flushed via
    `cur.executemany`, a final partial batch at EOF still flushes,
    and malformed lines are filtered before they enter the buffer
    so a bad row can never poison the surrounding pipelined INSERT.
    """

    def test_full_batch_flushes_in_single_executemany(self) -> None:
        # Exactly `_BATCH_SIZE` records produce exactly one
        # `executemany` call carrying all the rows -- one Postgres
        # round-trip instead of one per row is the whole point.
        with _sync_for_records(_sample_records(TEST_BATCH_SIZE)) as (
            _, analytics_sync,
        ):
            fake = _FakeConnection()
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, TEST_BATCH_SIZE):
                sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, TEST_BATCH_SIZE)
            self.assertEqual(sync_result.skipped_duplicate, 0)
            self.assertEqual(len(fake.batches), 1)
            sql, batch_rows = fake.batches[0]
            self.assertEqual(len(batch_rows), TEST_BATCH_SIZE)
            self.assertIn("ON CONFLICT (content_hash) DO NOTHING", sql)

    def test_rowcount_separates_inserted_duplicate(self) -> None:
        # Race-safe backstop: model a concurrent writer that landed
        # rows AFTER the startup pre-check completed but BEFORE the
        # batched flush, by holding the pre-check view empty while
        # seeding the DB-side `seen_hashes` set with the racing rows.
        # Every row reaches `executemany`; per-batch `cur.rowcount`
        # still tells the sync exactly how many were inserted vs.
        # ON-CONFLICT-skipped, so the `len(batch) - rowcount`
        # duplicate-math stays correct even when the pre-check missed
        # what the database already had.
        records = _sample_records(TEST_BATCH_SIZE + 1)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.pre_check_hashes = set()
            racing_records = records[:2]
            fake.seen_hashes.update(
                _sync_rows._content_hash(rec) for rec in racing_records
            )
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, len(records)):
                sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(
                sync_result.inserted, len(records) - len(racing_records),
            )
            self.assertEqual(
                sync_result.skipped_duplicate, len(racing_records),
            )
            self.assertEqual(len(fake.batches), 1)
            first_batch_records = fake.batches[0][1]
            self.assertEqual(len(first_batch_records), len(records))

    def test_final_partial_batch_flushed_at_eof(self) -> None:
        # 5 records with `_BATCH_SIZE=3` yields one full batch of 3
        # plus a trailing partial batch of 2 at EOF; both must
        # reach Postgres or a multi-thousand-record replay would
        # silently drop its tail.
        records = _sample_records(PARTIAL_BATCH_RECORD_COUNT)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, TEST_BATCH_SIZE):
                sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, len(records))
            self.assertEqual(sync_result.skipped_duplicate, 0)
            self.assertEqual(len(fake.batches), 2)
            self.assertEqual(len(fake.batches[0][1]), TEST_BATCH_SIZE)
            self.assertEqual(
                len(fake.batches[1][1]), len(records) - TEST_BATCH_SIZE,
            )
            # Two commits: one for the events insert, one after the
            # post-commit refresh of `analytics_daily_rollup`.
            self.assertEqual(fake.commit_called, 2)

    def test_smaller_than_batch_size_still_flushes(self) -> None:
        # Fewer records than `_BATCH_SIZE` still emit one partial
        # flush at EOF -- the no-rows-ever-reach-the-DB regression
        # is what makes this worth its own test even though
        # `test_final_partial_batch_flushed_at_eof` overlaps.
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, _LARGE_BATCH_SIZE):
                sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 1)
            self.assertEqual(len(fake.batches), 1)
            self.assertEqual(len(fake.batches[0][1]), 1)

    def test_malformed_lines_never_enter_batch(self) -> None:
        # Blank / non-JSON / missing-key lines are filtered in Python
        # before they reach the batch buffer; the `executemany` call
        # therefore carries only validated rows so a single bad line
        # cannot abort the surrounding batched INSERT.
        lines = [
            _record_line(issue=1),
            "",
            "not json",
            '{"ts": "2026-05-25T12:00:00+00:00", "repo": "o/r"}',
            _record_line(issue=2),
        ]
        with _sync_for_lines(lines) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 2)
            self.assertEqual(sync_result.skipped_malformed, 2)
            self.assertEqual(sync_result.total_lines, 5)
            self.assertEqual(len(fake.batches), 1)
            self.assertEqual(len(fake.batches[0][1]), 2)

    def test_no_records_skips_executemany(self) -> None:
        # A file with only blanks / malformed lines never builds a
        # batch and therefore never issues an `executemany` call --
        # the protocol stays quiet but the transaction still commits.
        with _sync_for_lines(["", "not json", "null"]) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(sync_result.inserted, 0)
            self.assertEqual(sync_result.skipped_malformed, 2)
            self.assertEqual(len(fake.batches), 0)
            # Two commits: events insert (no-op batch path still
            # commits to release the implicit transaction) + the
            # post-commit refresh hook that always fires on a
            # successful commit so a stale rollup recovers when the
            # operator reruns the sync.
            self.assertEqual(fake.commit_called, 2)


class AnalyticsSyncPreCheckTest(unittest.TestCase):
    """Startup `content_hash` pre-check: a single
    `SELECT content_hash FROM analytics_events WHERE content_hash IS
    NOT NULL` runs before the input file is opened so already-present
    rows are filtered in Python before they enter the batch buffer,
    intra-file duplicates are filtered against the same set so one
    JSONL with two identical records pays one round-trip not two, and
    pre-skipped rows never reach `executemany`. The batched INSERT ...
    ON CONFLICT (content_hash) DO NOTHING path stays the correctness
    backstop for the rare concurrent-writer race.
    """

    def test_select_runs_once_before_input_read(self) -> None:
        # A single SELECT against the unique content_hash index is the
        # whole startup tax; fan-out per row would defeat the point.
        with _sync_for_records(_sample_records(TEST_BATCH_SIZE)) as (
            _, analytics_sync,
        ):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
        select_sqls = _select_sqls(fake)
        self.assertEqual(len(select_sqls), 1)
        self.assertIn("SELECT content_hash", select_sqls[0])
        self.assertIn("analytics_events", select_sqls[0])
        self.assertIn("content_hash IS NOT NULL", select_sqls[0])

    def test_startup_skips_existing_hashes(self) -> None:
        # Seed the fake's database-state set with two of the three
        # records' hashes; the pre-check SELECT picks them up and the
        # in-Python filter skips them before the batch accumulator
        # sees them. Only the third record reaches the wire, and the
        # duplicates are counted via `skipped_duplicate` without any
        # per-row round-trip.
        records = _sample_records(TEST_BATCH_SIZE)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.seen_hashes.update(
                _sync_rows._content_hash(rec) for rec in records[:-1]
            )
            sync_result = _run_sync(analytics_sync, fake)
        self.assertEqual(sync_result.inserted, 1)
        self.assertEqual(sync_result.skipped_duplicate, len(records[:-1]))
        self.assertEqual(sync_result.total_lines, len(records))
        # The batched `executemany` only carries the new third record;
        # the two pre-skipped rows never enter the batch buffer.
        self.assertEqual(len(fake.batches), 1)
        self.assertEqual(len(fake.batches[0][1]), 1)
        batched_hashes = {row[-1] for row in fake.batches[0][1]}
        self.assertEqual(batched_hashes, {_sync_rows._content_hash(records[-1])})

    def test_duplicates_removed_before_batch_write(self) -> None:
        # Two identical records back-to-back in the same JSONL file
        # share a content_hash. The first occurrence is queued and
        # adds its hash to the in-Python skip set; the second hits the
        # set and is counted as `skipped_duplicate` without entering
        # the batch. The wire only sees one copy.
        duplicate = _sample_record(issue=1, event=_STAGE_ENTER)
        with _sync_for_records(
            [duplicate, duplicate, _sample_record(issue=2, event=_AGENT_EXIT)],
        ) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
        self.assertEqual(sync_result.inserted, 2)
        self.assertEqual(sync_result.skipped_duplicate, 1)
        self.assertEqual(sync_result.total_lines, 3)
        self.assertEqual(len(fake.batches), 1)
        self.assertEqual(len(fake.batches[0][1]), 2)
        # Each batched row carries a unique hash; the duplicate of
        # `duplicate` never made it past the in-Python filter.
        batched_hashes = [row[-1] for row in fake.batches[0][1]]
        self.assertEqual(len(set(batched_hashes)), len(batched_hashes))

    def test_pre_check_runs_against_empty_database(self) -> None:
        # The pre-check is unconditional but harmless when the
        # database is empty: every JSONL record still lands and the
        # SELECT just returns no rows.
        records = _sample_records(TEST_BATCH_SIZE)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
        self.assertEqual(len(_select_sqls(fake)), 1)
        self.assertEqual(sync_result.inserted, len(records))
        self.assertEqual(sync_result.skipped_duplicate, 0)
        self.assertEqual(len(fake.batches), 1)
        batch_rows = fake.batches[0][1]
        self.assertEqual(len(batch_rows), len(records))


class AnalyticsSyncProgressTest(unittest.TestCase):
    """Operator feedback for large replays: a progress record drops
    after every batched `executemany` flush (full or final partial)
    and a final "completed in %.3fs" line carries the wall-clock
    total. The defaults align `_BATCH_SIZE` with `_PROGRESS_INTERVAL`
    so each flush also drops one progress line on the existing
    cadence.
    """

    def test_progress_logged_per_batch_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / _LOG_FILENAME
            _, analytics_sync = _reload({
                _LOG_PATH_ENV: str(path),
                _DB_URL_ENV: _DB_URL,
            })
            interval = analytics_sync._PROGRESS_INTERVAL
            self.assertEqual(analytics_sync._BATCH_SIZE, interval)
            # Twice the configured batch size so the loop fills the
            # buffer twice with no partial-batch tail; distinct issues
            # keep the content hashes unique so the run exercises the
            # insert path rather than the dedup path.
            _write_jsonl(path, _sample_records(interval * 2))
            fake = _FakeConnection()
            _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        progress_lines = [
            line for line in log_lines if "progress lines=" in line
        ]
        # Two full-batch flushes -> two progress records (no partial
        # batch at EOF because the count divides the batch size).
        self.assertEqual(len(progress_lines), 2)
        # Per-batch flush fires AFTER the flush, so the line count at
        # each emission is the cumulative `total_lines` consumed up
        # to that flush.
        expected_total = interval * 2
        self.assertIn(f"lines={interval}", progress_lines[0])
        self.assertIn(f"lines={expected_total}", progress_lines[1])
        # The two batches together reach Postgres; the fake records
        # each `executemany` invocation in lockstep with the
        # progress lines.
        self.assertEqual(len(fake.batches), 2)

    def test_progress_fires_for_partial_final_batch(self) -> None:
        # A file whose row count does not divide `_BATCH_SIZE` still
        # emits a progress line for the partial flush at EOF -- an
        # operator's "did the tail land?" answer must not depend on a
        # round-number record count.
        records = _sample_records(PARTIAL_BATCH_RECORD_COUNT)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, TEST_BATCH_SIZE):
                _, log_lines = _sync_capturing_logs(self, analytics_sync, fake)
        progress_lines = [
            line for line in log_lines if "progress lines=" in line
        ]
        self.assertEqual(len(progress_lines), 2)
        self.assertIn(f"lines={TEST_BATCH_SIZE}", progress_lines[0])
        self.assertIn(f"inserted={TEST_BATCH_SIZE}", progress_lines[0])
        self.assertIn(f"lines={len(records)}", progress_lines[1])
        self.assertIn(f"inserted={len(records)}", progress_lines[1])

    def test_completed_log_carries_duration_s(self) -> None:
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result, log_lines = _sync_capturing_logs(
                self, analytics_sync, fake,
            )
        joined = "\n".join(log_lines)
        self.assertIn("completed in", joined)
        # The returned SyncResult carries the same wall-clock so the CLI
        # can print it without re-timing.
        self.assertGreaterEqual(sync_result.duration_s, 0.0)

    def test_no_op_paths_skip_connection_log(self) -> None:
        # `connect=lambda url: ...` must not be invoked when the sync
        # is a no-op; mirrors the existing AnalyticsSyncDisabledTest but
        # also confirms the new connecting/connected log lines do not
        # land in the no-op path (they imply a real connect was attempted).
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: _DB_URL,
        })
        # The disabled sink never dials `connect`, so a throwaway fake
        # stands in only to satisfy the shared runner signature.
        _, log_lines = _sync_capturing_logs(
            self, analytics_sync, _FakeConnection(),
        )
        joined = "\n".join(log_lines)
        self.assertNotIn("connecting to", joined)
        self.assertNotIn("connection established", joined)


class AnalyticsSyncDailyRollupRefreshTest(unittest.TestCase):
    """Every successful sync commit issues
    `REFRESH MATERIALIZED VIEW analytics_daily_rollup` so the
    rollup-backed dashboard widgets catch up to the new events.

    Two contract points the tests pin:
    - The refresh fires unconditionally on every successful commit
      (including all-duplicates and all-malformed runs that inserted
      zero new rows) so rerunning the sync is the documented recovery
      path for a stale rollup -- gating on `inserted > 0` would mean
      a refresh failure could only be recovered with a manual
      `REFRESH MATERIALIZED VIEW`.
    - A refresh exception is logged-and-swallowed so a pre-migration
      deployment or a transient Postgres error never aborts a sync
      whose events insert already committed.
    """

    def test_refresh_fires_after_successful_insert(self) -> None:
        with _sync_for_records([_sample_record(issue=1)]) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result, log_lines = _sync_capturing_logs(
                self, analytics_sync, fake,
            )
        self.assertEqual(sync_result.inserted, 1)
        self.assertEqual(len(_refresh_sqls(fake)), 1)
        self.assertIn("analytics_daily_rollup", _refresh_sqls(fake)[0])
        # Two commits: the events insert plus the post-refresh commit.
        self.assertEqual(fake.commit_called, 2)
        self.assertEqual(fake.rollback_called, 0)
        joined = "\n".join(log_lines)
        self.assertIn("refreshing materialized view", joined)
        self.assertIn("refreshed analytics_daily_rollup", joined)

    def test_refresh_fires_even_when_no_rows_inserted(self) -> None:
        # All-duplicates run: the pre-check filters both records, the
        # batch never reaches `executemany`, the events insert commit
        # is a no-op. The refresh still fires because rerunning the
        # sync is the documented recovery path for a stale rollup --
        # a prior sync whose refresh failed left the rollup behind,
        # and the operator must be able to recover by rerunning even
        # when the new JSONL file carries only duplicates.
        records = _sample_records(2)
        with _sync_for_records(records) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.seen_hashes.update(
                _sync_rows._content_hash(rec) for rec in records
            )
            sync_result = _run_sync(analytics_sync, fake)
        self.assertEqual(sync_result.inserted, 0)
        self.assertEqual(sync_result.skipped_duplicate, len(records))
        refresh_sqls = _refresh_sqls(fake)
        self.assertEqual(len(refresh_sqls), 1)
        self.assertIn("analytics_daily_rollup", refresh_sqls[0])
        # Two commits: events-insert (no-op batch path) + post-refresh.
        self.assertEqual(fake.commit_called, 2)

    def test_refresh_fires_on_malformed_only_files(self) -> None:
        # Defensive parallel to the all-duplicates path: a file of only
        # malformed lines also yields `inserted == 0`. The refresh
        # still fires for the same recovery reason -- the JSONL file's
        # contents do not determine whether the operator needs a
        # rollup refresh.
        with _sync_for_lines(["not json", "null"]) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result = _run_sync(analytics_sync, fake)
        self.assertEqual(sync_result.inserted, 0)
        self.assertEqual(len(_refresh_sqls(fake)), 1)

    def test_refresh_failure_does_not_abort_sync(self) -> None:
        # A REFRESH failure -- the MV not migrated yet on a
        # pre-migration deployment, a transient lock-wait error -- is
        # logged and swallowed. The committed insert is durable
        # regardless, so the sync still returns success.
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.raise_on_refresh = RuntimeError(
                "materialized view does not exist"
            )
            sync_result, log_lines = _sync_capturing_logs(
                self, analytics_sync, fake,
            )
        # Sync completed successfully despite the refresh raising.
        self.assertEqual(sync_result.inserted, 1)
        # Only the events-insert commit landed; the post-refresh commit
        # was never reached because execute raised first.
        self.assertEqual(fake.commit_called, 1)
        # Refresh-side rollback ran to clear the aborted transaction
        # so the connection can be cleanly closed.
        self.assertEqual(fake.rollback_called, 1)
        self.assertEqual(fake.close_called, 1)
        joined = "\n".join(log_lines)
        self.assertIn("refresh of analytics_daily_rollup failed", joined)
        # The original "completed in" summary still fires so an
        # operator scraping log lines sees the sync as successful.
        self.assertIn("completed in", joined)

    def test_refresh_skipped_in_no_op_path(self) -> None:
        # `connect` is not invoked when either knob disables the sync,
        # so no SQL of any kind -- including REFRESH -- ever runs.
        # Mirrors the existing `AnalyticsSyncDisabledTest` for the
        # refresh surface.
        _, analytics_sync = _reload({
            _LOG_PATH_ENV: _SENTINEL_DISABLED,
            _DB_URL_ENV: _DB_URL,
        })
        connected: list[str] = []
        analytics_sync.sync_jsonl_to_postgres(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])


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
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"psycopg not available: {exc}")

    def test_real_postgres_insert_and_dedup(self) -> None:
        self._apply_schema()
        records = [
            _sample_record(issue=1, event=_STAGE_ENTER, stage="ready"),
            _sample_record(issue=2, event=_AGENT_EXIT, duration_s=3.0),
            _sample_record(issue=3, event="stage_evaluation",
                           stage="validating", duration_s=1.5,
                           result="ok"),
        ]
        with _sync_for_records(records, db_url=self.db_url) as (
            _, analytics_sync,
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
        import psycopg

        self._apply_schema()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / _LOG_FILENAME
            agent_run = _sample_record(
                issue=42,
                event=_AGENT_EXIT,
                stage=_STAGE_IMPLEMENTING,
                agent_role="developer",
                backend="codex",
                review_round=4,
                retry_count=1,
                duration_s=12.5,
                exit_code=0,
                timed_out=False,
                input_tokens=300,
                output_tokens=150,
                cached_tokens=50,
                cache_read_tokens=20,
                cache_write_tokens=10,
                models=["gpt-5-codex"],
                cost_usd=0.0042,
                cost_source="estimated",
            )
            _write_jsonl(path, [agent_run])
            _, analytics_sync = _reload({
                _LOG_PATH_ENV: str(path),
                _DB_URL_ENV: self.db_url,
            })
            sync_result = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(sync_result.inserted, 1)

            with psycopg.connect(self.db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT model, total_tokens, total_cache_tokens, "
                        "review_round_bucket, failed, has_cost, cost_source "
                        "FROM analytics_agent_runs WHERE issue = %s",
                        (agent_run[_ISSUE_KEY],),
                    )
                    row = cur.fetchone()
        self.assertIsNotNone(row)
        projection = _AgentRunProjection(*row)
        self.assertEqual(projection.model, agent_run["models"][0])
        self.assertEqual(
            projection.total_tokens,
            agent_run["input_tokens"] + agent_run["output_tokens"],
        )
        self.assertEqual(
            projection.total_cache,
            agent_run["cached_tokens"]
            + agent_run["cache_read_tokens"]
            + agent_run["cache_write_tokens"],
        )
        self.assertEqual(projection.bucket, "3-5")
        self.assertFalse(projection.failed)
        self.assertTrue(projection.has_cost)
        self.assertEqual(projection.cost_source, "estimated")

    def test_daily_rollup_refreshes_after_sync(self) -> None:
        # End-to-end Layer 4: insert two `agent_exit` rows on the same
        # UTC day with matching key columns, run the sync (which triggers
        # the post-commit `REFRESH MATERIALIZED VIEW`), and assert the
        # rollup row carries the summed token / cost / duration columns
        # and the failure / timeout counts the dashboard's reliability
        # tiles read off. A column typo or a wrong CASE predicate would
        # compile-fail here even if the text regexes in
        # `tests/test_analytics_schema.py` still matched.
        import psycopg

        self._apply_schema()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / _LOG_FILENAME
            successful_run = _sample_record(
                issue=7,
                event=_AGENT_EXIT,
                stage=_STAGE_IMPLEMENTING,
                backend="claude",
                cost_source="reported",
                duration_s=4.0,
                exit_code=0,
                timed_out=False,
                input_tokens=100,
                output_tokens=50,
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
                duration_s=6.0,
                exit_code=1,
                timed_out=True,
                input_tokens=200,
                output_tokens=80,
                cached_tokens=10,
                cache_read_tokens=4,
                cache_write_tokens=1,
                cost_usd=0.2,
            )
            runs = [successful_run, failed_run]
            _write_jsonl(path, runs)
            _, analytics_sync = _reload({
                _LOG_PATH_ENV: str(path),
                _DB_URL_ENV: self.db_url,
            })
            sync_result = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(sync_result.inserted, len(runs))

            with psycopg.connect(self.db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT total_input_tokens, total_output_tokens, "
                        "total_cached_tokens, total_cache_read_tokens, "
                        "total_cache_write_tokens, total_cost_usd, "
                        "duration_s_sum, duration_s_count, "
                        "failed_count, timed_out_count, event_count "
                        "FROM analytics_daily_rollup "
                        "WHERE issue = %s",
                        (successful_run[_ISSUE_KEY],),
                    )
                    row = cur.fetchone()
        self.assertIsNotNone(row)
        projection = _DailyRollupProjection(*row)
        self.assertEqual(
            projection.total_in, sum(run["input_tokens"] for run in runs),
        )
        self.assertEqual(
            projection.total_out, sum(run["output_tokens"] for run in runs),
        )
        self.assertEqual(
            projection.total_cached, sum(run["cached_tokens"] for run in runs),
        )
        self.assertEqual(
            projection.total_cache_read,
            sum(run["cache_read_tokens"] for run in runs),
        )
        self.assertEqual(
            projection.total_cache_write,
            sum(run["cache_write_tokens"] for run in runs),
        )
        # Numeric comparison: the schema uses NUMERIC(20, 10), so the
        # sum may come back as a Decimal. Cast both sides to float for
        # the comparison so an exact-decimal mismatch on the literal
        # does not blow up the assertion.
        self.assertAlmostEqual(
            float(projection.total_cost),
            sum(run["cost_usd"] for run in runs),
            places=6,
        )
        self.assertEqual(
            projection.duration_sum,
            sum(run["duration_s"] for run in runs),
        )
        self.assertEqual(projection.duration_count, len(runs))
        self.assertEqual(
            projection.failed_count,
            sum(run["exit_code"] != 0 for run in runs),
        )
        self.assertEqual(
            projection.timed_out_count,
            sum(run["timed_out"] for run in runs),
        )
        self.assertEqual(projection.event_count, len(runs))

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


# Every compatibility member and its responsibility-named implementation leaf.
_MAPPING_MEMBER_MODULES = {
    "_build_insert_sql": "orchestrator.analytics._sync_row_mapping",
    "_content_hash": "orchestrator.analytics._sync_row_parse",
    "_prepare_record": "orchestrator.analytics._sync_row_mapping",
    "_row_values": "orchestrator.analytics._sync_row_mapping",
    "_RowProvenance": "orchestrator.analytics._sync_row_mapping",
}

# The subset the ingest driver imports for its own use, so `sync.<name>`
# resolves to the same object. `_content_hash` / `_PROMOTED_COLUMNS` are
# reached only through `_sync_rows` (the driver does not use them), so the
# driver carries no alias for them.
_SYNC_DRIVEN_MEMBERS = (
    "_build_insert_sql",
    "_prepare_record",
    "_row_values",
    "_RowProvenance",
)


class SyncRowMappingExtractionTest(unittest.TestCase):
    """The record -> DB-row mapping (the promoted-column schema, the
    canonical-JSON content hash, and per-record validation) lives in focused
    parsing and mapping leaves. The `_sync_rows` compatibility hub and ingest
    driver retain the historical objects.
    """

    def test_mapping_members_live_in_responsibility_named_leaves(self) -> None:
        for name, module_name in _MAPPING_MEMBER_MODULES.items():
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(_sync_rows, name).__module__,
                    module_name,
                )

    def test_sync_reaches_the_sync_rows_objects(self) -> None:
        from orchestrator.analytics import sync
        for name in _SYNC_DRIVEN_MEMBERS:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(sync, name), getattr(_sync_rows, name)
                )


if __name__ == "__main__":
    unittest.main()
