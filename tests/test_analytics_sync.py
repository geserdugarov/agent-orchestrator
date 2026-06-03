# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

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
from unittest.mock import patch


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
        sys.modules.pop("orchestrator.analytics.sync", None)
        import orchestrator.analytics as analytics
        import orchestrator.analytics.sync as analytics_sync
        return analytics, analytics_sync


class _FakeCursor:
    """Records every (sql, params) executed and emulates ON CONFLICT.

    Implemented as a context manager so the production `with
    conn.cursor() as cur:` block works unchanged. `rowcount` mirrors
    psycopg's "1 on insert, 0 on conflict" convention; tests assert
    against the recorded inserts.
    """

    def __init__(self, store: "_FakeConnection") -> None:
        self._store = store
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        # Hash is the last param; relies on the column order baked
        # into `_build_insert_sql`. If the schema's column order ever
        # changes the test will fail loudly here -- which is fine,
        # the test would be wrong in lock-step with the production
        # code.
        content_hash = params[-1]
        if content_hash in self._store.seen_hashes:
            self.rowcount = 0
            self._store.duplicate_calls.append((sql, params))
        else:
            self._store.seen_hashes.add(content_hash)
            self._store.inserts.append((sql, params))
            self.rowcount = 1


class _FakeConnection:
    """In-memory stand-in for a psycopg connection.

    Captures inserts and conflict-skips, plus commit / rollback /
    close calls so tests can assert that the sync commits on success
    and rolls back on error.
    """

    def __init__(self) -> None:
        self.inserts: list[tuple[str, tuple]] = []
        self.duplicate_calls: list[tuple[str, tuple]] = []
        self.seen_hashes: set[str] = set()
        self.commit_called = 0
        self.rollback_called = 0
        self.close_called = 0
        self.raise_on_execute: Exception | None = None

    def cursor(self) -> _FakeCursor:
        if self.raise_on_execute is not None:
            cur = _FakeCursor(self)

            def _raise(sql: str, params: tuple) -> None:
                raise self.raise_on_execute  # type: ignore[misc]

            cur.execute = _raise  # type: ignore[method-assign]
            return cur
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_called += 1

    def rollback(self) -> None:
        self.rollback_called += 1

    def close(self) -> None:
        self.close_called += 1


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Mirror `analytics.append_record`'s on-disk encoding so the
    content hash the sync computes matches what a real writer would
    produce.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _sample_record(
    *,
    issue: int = 1,
    event: str = "stage_enter",
    ts: str = "2026-05-25T12:00:00+00:00",
    **extras,
) -> dict:
    rec = {
        "ts": ts,
        "repo": "owner/repo",
        "issue": issue,
        "event": event,
    }
    rec.update(extras)
    return rec


class AnalyticsDbUrlConfigTest(unittest.TestCase):
    """`ANALYTICS_DB_URL` parses at import inside the analytics
    package: empty / sentinel disables; a real URL passes through
    verbatim so a libpq URL is the single-knob endpoint contract.
    """

    def test_default_is_disabled(self) -> None:
        analytics, _ = _reload()
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_empty_string_disables(self) -> None:
        analytics, _ = _reload({"ANALYTICS_DB_URL": ""})
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_sentinel_values_disable(self) -> None:
        for value in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(value=value):
                analytics, _ = _reload({"ANALYTICS_DB_URL": value})
                self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_real_url_passes_through(self) -> None:
        url = "postgresql://u:p@db.example.com:5432/orchestrator_analytics"
        analytics, _ = _reload({"ANALYTICS_DB_URL": url})
        self.assertEqual(analytics.ANALYTICS_DB_URL, url)

    def test_whitespace_stripped(self) -> None:
        analytics, _ = _reload(
            {"ANALYTICS_DB_URL": "  postgresql://h/db  "}
        )
        self.assertEqual(analytics.ANALYTICS_DB_URL, "postgresql://h/db")


class AnalyticsSyncDisabledTest(unittest.TestCase):
    """When either env knob is unset the sync is a silent no-op: no
    connection attempt, no row insertion, no error. Mirrors how
    `analytics.append_record` no-ops when the sink is disabled.
    """

    def test_no_op_when_db_url_unset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "",
            })
            connected = []
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(result.inserted, 0)
            self.assertEqual(result.total_lines, 0)

    def test_no_op_when_log_path_unset(self) -> None:
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "postgresql://h/db",
        })
        connected = []
        result = analytics_sync.sync_jsonl_to_postgres(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result.inserted, 0)

    def test_no_op_when_log_file_missing(self) -> None:
        # Configured but file not created yet (orchestrator hasn't
        # emitted any record). Don't connect, don't fail.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            connected = []
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(result.inserted, 0)


class AnalyticsSyncInsertTest(unittest.TestCase):
    """Happy-path inserts: each well-formed JSONL line becomes one
    INSERT carrying the promoted columns + extras + content_hash; the
    transaction commits on success.
    """

    def test_inserts_each_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(issue=1, event="stage_enter", stage="implementing"),
                _sample_record(issue=2, event="agent_exit", duration_s=12.5),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 2)
            self.assertEqual(result.skipped_duplicate, 0)
            self.assertEqual(result.skipped_malformed, 0)
            self.assertEqual(result.total_lines, 2)
            self.assertEqual(len(fake.inserts), 2)
            self.assertEqual(fake.commit_called, 1)
            self.assertEqual(fake.rollback_called, 0)
            self.assertEqual(fake.close_called, 1)

    def test_promoted_columns_and_extras_split(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(
                    event="agent_exit",
                    stage="implementing",
                    duration_s=42.0,
                    backend="claude",
                    session_id="sess-abc",
                    input_tokens=100,
                    custom_future_key="something-new",
                ),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            sql, params = fake.inserts[0]
            promoted = analytics_sync._PROMOTED_COLUMNS
            self.assertEqual(params[promoted.index("repo")], "owner/repo")
            self.assertEqual(params[promoted.index("issue")], 1)
            self.assertEqual(params[promoted.index("event")], "agent_exit")
            self.assertEqual(params[promoted.index("stage")], "implementing")
            self.assertEqual(params[promoted.index("backend")], "claude")
            self.assertEqual(params[promoted.index("session_id")], "sess-abc")
            self.assertEqual(params[promoted.index("input_tokens")], 100)
            # Extras column lives after the promoted block.
            extras_idx = len(promoted)
            self.assertEqual(
                params[extras_idx], {"custom_future_key": "something-new"}
            )
            # source_path / source_line / content_hash trail it.
            self.assertEqual(params[extras_idx + 1], str(path))
            self.assertEqual(params[extras_idx + 2], 1)
            # Content hash matches the canonical encoding of the source
            # record, not the unsorted one we passed in -- this is
            # what makes dedup robust against prune-induced rewrites.
            self.assertIsInstance(params[extras_idx + 3], str)
            self.assertEqual(len(params[extras_idx + 3]), 64)

    def test_ts_parsed_to_datetime(self) -> None:
        # The ts column is TIMESTAMPTZ; psycopg expects a datetime,
        # not a string. A naive string would be silently inserted as
        # text in some configurations.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record(ts="2026-05-25T12:00:00+00:00")])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            _, params = fake.inserts[0]
            ts_value = params[analytics_sync._PROMOTED_COLUMNS.index("ts")]
            self.assertIsInstance(ts_value, datetime)
            self.assertIsNotNone(ts_value.tzinfo)


class AnalyticsSyncDedupTest(unittest.TestCase):
    """Repeated runs over the same file insert each record exactly
    once. This is the core idempotency guarantee the issue calls
    out.
    """

    def test_second_run_inserts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(issue=1),
                _sample_record(issue=2),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            first = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            second = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(first.inserted, 2)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, 2)
            # Only the 2 originals are durably persisted.
            self.assertEqual(len(fake.inserts), 2)

    def test_post_prune_renumbering_does_not_duplicate(self) -> None:
        # The realistic post-prune scenario: file had 3 records, the
        # prune dropped record #1, leaving #2 + #3 at line numbers 1
        # and 2. A naive (source_path, source_line) key would
        # re-insert them under the freed (path, 1) / (path, 2) keys.
        # Content-hash dedup keeps them out.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(issue=1, event="a"),
                _sample_record(issue=2, event="b"),
                _sample_record(issue=3, event="c"),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            # Operator runs prune; file now has only #2 + #3 at lines 1 + 2.
            _write_jsonl(path, [
                _sample_record(issue=2, event="b"),
                _sample_record(issue=3, event="c"),
            ])
            second = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, 2)


class AnalyticsSyncMalformedTest(unittest.TestCase):
    """Malformed lines mirror the prune helper's tolerance: blanks are
    silently skipped, garbage / missing keys are counted and logged
    but never abort the sync. The JSONL file is never rewritten.
    """

    def test_blank_lines_are_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write("\n")
                fh.write(json.dumps(_sample_record(), sort_keys=True) + "\n")
                fh.write("   \n")
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.skipped_malformed, 0)
            self.assertEqual(result.total_lines, 3)

    def test_non_json_line_counted_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write("this is not json\n")
                fh.write(json.dumps(_sample_record(), sort_keys=True) + "\n")
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.skipped_malformed, 1)
            self.assertEqual(result.malformed_line_numbers, (1,))
            # The good record on line 2 still gets inserted -- one bad
            # line cannot poison the whole sync.
            self.assertEqual(len(fake.inserts), 1)

    def test_json_non_object_skipped(self) -> None:
        # `null`, lists, numbers parse cleanly but aren't dict
        # records; treat them as malformed rather than crashing.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write("null\n")
                fh.write("[1, 2, 3]\n")
                fh.write("42\n")
                fh.write(json.dumps(_sample_record(), sort_keys=True) + "\n")
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.skipped_malformed, 3)

    def test_missing_required_key_skipped(self) -> None:
        # Records missing `ts` / `repo` / `issue` / `event` cannot be
        # inserted (NOT NULL columns) so the sync filters them out
        # rather than letting psycopg raise mid-transaction.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write('{"repo": "o/r", "issue": 1, "event": "x"}\n')  # missing ts
                fh.write('{"ts": "2026-05-25T12:00:00+00:00", "issue": 1, "event": "x"}\n')  # missing repo
                fh.write('{"ts": "2026-05-25T12:00:00+00:00", "repo": "o/r", "event": "x"}\n')  # missing issue
                fh.write('{"ts": "2026-05-25T12:00:00+00:00", "repo": "o/r", "issue": 1}\n')  # missing event
                fh.write(json.dumps(_sample_record(), sort_keys=True) + "\n")
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.skipped_malformed, 4)

    def test_unparseable_ts_skipped(self) -> None:
        # Parallel to `prune_old_records`'s behavior on a garbled `ts`:
        # the record is preserved verbatim in the JSONL file (sync is
        # read-only) but is not inserted.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write('{"ts": "not-a-date", "repo": "o/r", "issue": 1, "event": "x"}\n')
                fh.write(json.dumps(_sample_record(), sort_keys=True) + "\n")
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.skipped_malformed, 1)
            # File untouched -- the sync never rewrites; operator
            # cleanup is the same as for `prune_old_records`.
            preserved = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(preserved), 2)

    def test_naive_ts_treated_as_utc(self) -> None:
        # Same forward-compat as `prune_old_records`: records written
        # by an older writer without tz info are interpreted as UTC
        # rather than being rejected as malformed.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            naive = "2026-05-25T12:00:00"
            _write_jsonl(path, [_sample_record(ts=naive)])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: fake,
                json_adapter=lambda v: v,
            )
            self.assertEqual(result.inserted, 1)
            _, params = fake.inserts[0]
            ts_value = params[analytics_sync._PROMOTED_COLUMNS.index("ts")]
            self.assertEqual(ts_value.tzinfo, timezone.utc)


class AnalyticsSyncTransactionTest(unittest.TestCase):
    """A driver-side error mid-stream rolls the transaction back so
    a partial batch is never committed. The exception propagates so
    the CLI surfaces a non-zero exit code rather than reporting
    "success" on a half-inserted batch.
    """

    def test_execute_error_rolls_back_and_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            fake.raise_on_execute = RuntimeError("simulated driver failure")
            with self.assertRaises(RuntimeError):
                analytics_sync.sync_jsonl_to_postgres(
                    connect=lambda url: fake,
                    json_adapter=lambda v: v,
                )
            self.assertEqual(fake.commit_called, 0)
            self.assertEqual(fake.rollback_called, 1)
            self.assertEqual(fake.close_called, 1)


class AnalyticsSyncCliTest(unittest.TestCase):
    """The CLI prints a one-line summary on success and exits 1 on
    failure so a cron / systemd unit can surface the error.
    """

    def test_cli_no_op_prints_zeros(self) -> None:
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "",
        })
        buf = io.StringIO()
        with patch("sys.stdout", buf):
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
                "ANALYTICS_LOG_PATH": "off",
                "ANALYTICS_DB_URL": "",
            })

            def fake_sync(*, log_path, db_url, **kwargs):
                # Verify the CLI threads the overrides through.
                self.assertEqual(log_path, path)
                self.assertEqual(db_url, "postgresql://override/db")
                return analytics_sync.SyncResult(inserted=1, total_lines=1)

            with patch.object(
                analytics_sync, "sync_jsonl_to_postgres", side_effect=fake_sync
            ):
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = analytics_sync.main([
                        "--log-path", str(path),
                        "--db-url", "postgresql://override/db",
                    ])
            self.assertEqual(rc, 0)
            self.assertIn("inserted=1", buf.getvalue())

    def test_cli_surfaces_failure_as_nonzero(self) -> None:
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "",
        })
        with patch.object(
            analytics_sync,
            "sync_jsonl_to_postgres",
            side_effect=RuntimeError("boom"),
        ):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = analytics_sync.main([])
        self.assertEqual(rc, 1)

    def test_cli_logs_and_stdout_share_utc_clock(self) -> None:
        # Regression for the reviewer's TZ-skew finding: log lines used
        # to print in local time while the stdout summary printed UTC,
        # so on a TZ+7 host the two surfaces were 7 hours apart for the
        # same event. With both pinned to UTC + an explicit "UTC"
        # marker, mixing stdout/stderr stays a coherent time stream.
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "",
        })
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        # Patch BEFORE main() so the StreamHandler that
        # `_configure_cli_logging` constructs captures the patched
        # stderr (StreamHandler() resolves `sys.stderr` at __init__).
        try:
            with patch("sys.stderr", err_buf), patch("sys.stdout", out_buf):
                rc = analytics_sync.main([])
        finally:
            # Restore the root logger so a UTC handler doesn't leak
            # into other tests in the same process.
            root = logging.getLogger()
            for h in list(root.handlers):
                root.removeHandler(h)
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
            delta, 5,
            f"stdout and stderr timestamps disagree by {delta}s: "
            f"out={out_match.group(1)} err={err_match.group(1)}",
        )
        # Cross-check against `now()` to confirm the shared clock is
        # actually UTC, not just any single tz. A local-time formatter
        # would land outside this window on a TZ-skewed host.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(
            abs((out_ts - now_utc).total_seconds()), 5,
            "stdout summary timestamp is not UTC",
        )
        self.assertLess(
            abs((err_ts - now_utc).total_seconds()), 5,
            "log timestamp is not UTC",
        )

    def test_cli_stdout_carries_timestamp_and_duration(self) -> None:
        # Operators run the sync from a terminal and expect a timestamped,
        # one-line summary with the elapsed wall-clock so a multi-thousand
        # record replay surfaces its cost without grepping the log lines.
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "",
        })
        buf = io.StringIO()
        with patch("sys.stdout", buf):
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
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://u:secret@h:5432/db",
            })
            fake = _FakeConnection()
            with self.assertLogs("orchestrator.analytics.sync", level="INFO") as cm:
                analytics_sync.sync_jsonl_to_postgres(
                    connect=lambda url: fake,
                    json_adapter=lambda v: v,
                )
        joined = "\n".join(cm.output)
        self.assertIn("connecting to", joined)
        self.assertIn("connection established", joined)
        # The credential half of the URL must never appear; the redacted
        # form keeps the scheme + host + db so the operator can still
        # confirm which endpoint they hit.
        self.assertNotIn("secret", joined)
        self.assertNotIn("u:secret", joined)
        self.assertIn("***@h:5432", joined)

    def test_redact_db_url_without_credentials_passes_through(self) -> None:
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

    def test_redact_db_url_strips_query_string_password(self) -> None:
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

    def test_redact_db_url_strips_query_string_sslpassword(self) -> None:
        # `sslpassword` decrypts the SSL client key; same threat model
        # as `password` itself.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url(
            "postgresql://h/db?sslpassword=ssl-secret"
        )
        self.assertNotIn("ssl-secret", redacted)
        self.assertIn("sslpassword=", redacted)

    def test_redact_db_url_query_params_case_insensitive(self) -> None:
        # libpq treats parameter names as case-insensitive; uppercase
        # spellings must redact identically so a `?PASSWORD=secret`
        # URL does not slip past the filter.
        _, analytics_sync = _reload()
        redacted = analytics_sync._redact_db_url(
            "postgresql://h/db?PASSWORD=secret"
        )
        self.assertNotIn("secret", redacted)

    def test_connect_log_redacts_query_string_password(self) -> None:
        # End-to-end regression: a query-string-password URL must not
        # leak the password into the connection log.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": (
                    "postgresql://h:5432/db?user=u&password=qs-secret"
                ),
            })
            fake = _FakeConnection()
            with self.assertLogs(
                "orchestrator.analytics.sync", level="INFO"
            ) as cm:
                analytics_sync.sync_jsonl_to_postgres(
                    connect=lambda url: fake,
                    json_adapter=lambda v: v,
                )
        joined = "\n".join(cm.output)
        self.assertNotIn("qs-secret", joined)
        self.assertIn("connection established", joined)


class AnalyticsSyncProgressTest(unittest.TestCase):
    """Operator feedback for large replays: a progress record drops every
    `_PROGRESS_INTERVAL` lines consumed (counted across inserted, skipped,
    and blank lines so a malformed-heavy file still advances) and a final
    "completed in %.3fs" line carries the wall-clock total.
    """

    def test_progress_logged_each_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            # Build twice the progress interval so the loop crosses the
            # boundary at least twice; distinct `issue` values keep the
            # content hashes (and therefore the row identities) unique
            # so the run exercises the insert path rather than the
            # dedup path.
            interval = analytics_sync._PROGRESS_INTERVAL
            records = [_sample_record(issue=i) for i in range(1, interval * 2 + 1)]
            _write_jsonl(path, records)
            fake = _FakeConnection()
            with self.assertLogs("orchestrator.analytics.sync", level="INFO") as cm:
                analytics_sync.sync_jsonl_to_postgres(
                    connect=lambda url: fake,
                    json_adapter=lambda v: v,
                )
        progress_lines = [m for m in cm.output if "progress lines=" in m]
        # Two interval crossings -> two progress records (the final line
        # number equals 2 * interval, which is a multiple of interval).
        self.assertEqual(len(progress_lines), 2)
        # The first crossing's count should be exactly the interval, not
        # an off-by-one -- a bad % check would emit at lines=interval+1
        # or lines=interval-1.
        self.assertIn(f"lines={interval}", progress_lines[0])
        self.assertIn(f"lines={interval * 2}", progress_lines[1])

    def test_completed_log_carries_duration_s(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": "postgresql://h/db",
            })
            fake = _FakeConnection()
            with self.assertLogs("orchestrator.analytics.sync", level="INFO") as cm:
                result = analytics_sync.sync_jsonl_to_postgres(
                    connect=lambda url: fake,
                    json_adapter=lambda v: v,
                )
        joined = "\n".join(cm.output)
        self.assertIn("completed in", joined)
        # The returned SyncResult carries the same wall-clock so the CLI
        # can print it without re-timing.
        self.assertGreaterEqual(result.duration_s, 0.0)

    def test_no_op_paths_skip_connection_log(self) -> None:
        # `connect=lambda url: ...` must not be invoked when the sync
        # is a no-op; mirrors the existing AnalyticsSyncDisabledTest but
        # also confirms the new connecting/connected log lines do not
        # land in the no-op path (they imply a real connect was attempted).
        _, analytics_sync = _reload({
            "ANALYTICS_LOG_PATH": "off",
            "ANALYTICS_DB_URL": "postgresql://h/db",
        })
        with self.assertLogs("orchestrator.analytics.sync", level="INFO") as cm:
            analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: _FakeConnection(),
            )
        joined = "\n".join(cm.output)
        self.assertNotIn("connecting to", joined)
        self.assertNotIn("connection established", joined)


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

    DB_URL_ENV = "ANALYTICS_TEST_DB_URL"

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_url = os.environ.get(cls.DB_URL_ENV, "").strip()
        if not cls.db_url:
            raise unittest.SkipTest(
                f"{cls.DB_URL_ENV} not set; live Postgres integration "
                "test skipped. Set it to a libpq URL pointing at the "
                "compose service (or any disposable Postgres) to run."
            )
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f"psycopg not available: {e}")

    def _apply_schema(self) -> None:
        import psycopg

        repo_root = Path(__file__).resolve().parent.parent
        schema_path = repo_root / "analytics-db" / "init" / "01-schema.sql"
        with psycopg.connect(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_path.read_text(encoding="utf-8"))
                cur.execute("TRUNCATE analytics_events RESTART IDENTITY")
            conn.commit()

    def _row_count(self) -> int:
        import psycopg

        with psycopg.connect(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM analytics_events")
                row = cur.fetchone()
        return int(row[0]) if row else 0

    def test_real_postgres_insert_and_dedup(self) -> None:
        self._apply_schema()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(issue=1, event="stage_enter", stage="ready"),
                _sample_record(issue=2, event="agent_exit", duration_s=3.0),
                _sample_record(issue=3, event="stage_evaluation",
                               stage="validating", duration_s=1.5,
                               result="ok"),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": self.db_url,
            })
            first = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(first.inserted, 3)
            self.assertEqual(first.skipped_duplicate, 0)
            self.assertEqual(self._row_count(), 3)

            second = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped_duplicate, 3)
            self.assertEqual(self._row_count(), 3)

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
            path = Path(td) / "a.jsonl"
            _write_jsonl(path, [
                _sample_record(
                    issue=42,
                    event="agent_exit",
                    stage="implementing",
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
                ),
            ])
            _, analytics_sync = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_DB_URL": self.db_url,
            })
            result = analytics_sync.sync_jsonl_to_postgres()
            self.assertEqual(result.inserted, 1)

            with psycopg.connect(self.db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT model, total_tokens, total_cache_tokens, "
                        "review_round_bucket, failed, has_cost, cost_source "
                        "FROM analytics_agent_runs WHERE issue = 42"
                    )
                    row = cur.fetchone()
        self.assertIsNotNone(row)
        (
            model, total_tokens, total_cache, bucket,
            failed, has_cost, cost_source,
        ) = row
        self.assertEqual(model, "gpt-5-codex")
        self.assertEqual(total_tokens, 450)
        self.assertEqual(total_cache, 80)
        self.assertEqual(bucket, "3-5")
        self.assertFalse(failed)
        self.assertTrue(has_cost)
        self.assertEqual(cost_source, "estimated")


if __name__ == "__main__":
    unittest.main()
