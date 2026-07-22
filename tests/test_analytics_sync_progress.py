# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync progress-reporting tests."""

import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch


from tests.analytics_sync_execution import (
    sync_capturing_logs as _sync_capturing_logs,
)


from tests.analytics_sync_reload import (
    reload_sync as _reload,
    sync_for_records as _sync_for_records,
)


from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)


from tests.analytics_sync_payloads import (
    write_jsonl as _write_jsonl,
    sample_record as _sample_record,
    sample_records as _sample_records,
)


TEST_BATCH_SIZE = 3


PARTIAL_BATCH_RECORD_COUNT = TEST_BATCH_SIZE + 2


_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"


_DB_URL_ENV = "ANALYTICS_DB_URL"


_SENTINEL_DISABLED = "off"


_DB_URL = "postgresql://h/db"


_BATCH_SIZE_ATTR = "_BATCH_SIZE"


_LOG_FILENAME = "a.jsonl"


@dataclass(frozen=True)
class _ProgressCapture:
    interval: int
    connection: _FakeConnection
    log_lines: list[str]

    @property
    def progress_lines(self) -> list[str]:
        return [line for line in self.log_lines if "progress lines=" in line]


def _capture_progress(test_case, path: Path) -> _ProgressCapture:
    analytics_sync = _reload(
        {
            _LOG_PATH_ENV: str(path),
            _DB_URL_ENV: _DB_URL,
        }
    )[1]
    interval = analytics_sync._PROGRESS_INTERVAL
    test_case.assertEqual(analytics_sync._BATCH_SIZE, interval)
    _write_jsonl(path, _sample_records(interval * 2))
    connection = _FakeConnection()
    _, log_lines = _sync_capturing_logs(test_case, analytics_sync, connection)
    return _ProgressCapture(interval, connection, log_lines)


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
            # Twice the configured batch size so the loop fills the
            # buffer twice with no partial-batch tail; distinct issues
            # keep the content hashes unique so the run exercises the
            # insert path rather than the dedup path.
            capture = _capture_progress(self, path)
        # Two full-batch flushes -> two progress records (no partial
        # batch at EOF because the count divides the batch size).
        self.assertEqual(len(capture.progress_lines), 2)
        # Per-batch flush fires AFTER the flush, so the line count at
        # each emission is the cumulative `total_lines` consumed up
        # to that flush.
        expected_total = capture.interval * 2
        self.assertIn(f"lines={capture.interval}", capture.progress_lines[0])
        self.assertIn(f"lines={expected_total}", capture.progress_lines[1])
        # The two batches together reach Postgres; the fake records
        # each `executemany` invocation in lockstep with the
        # progress lines.
        self.assertEqual(len(capture.connection.batches), 2)

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
        progress_lines = [line for line in log_lines if "progress lines=" in line]
        self.assertEqual(len(progress_lines), 2)
        self.assertIn(f"lines={TEST_BATCH_SIZE}", progress_lines[0])
        self.assertIn(f"inserted={TEST_BATCH_SIZE}", progress_lines[0])
        self.assertIn(f"lines={len(records)}", progress_lines[1])
        self.assertIn(f"inserted={len(records)}", progress_lines[1])

    def test_completed_log_carries_duration_s(self) -> None:
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            sync_result, log_lines = _sync_capturing_logs(
                self,
                analytics_sync,
                fake,
            )
        joined = "\n".join(log_lines)
        self.assertIn("completed in", joined)
        # The returned SyncResult carries the same wall-clock so the CLI
        # can print it without re-timing.
        self.assertGreaterEqual(sync_result.duration_s, float())

    def test_no_op_paths_skip_connection_log(self) -> None:
        # `connect=lambda url: ...` must not be invoked when the sync
        # is a no-op; mirrors the existing AnalyticsSyncDisabledTest but
        # also confirms the new connecting/connected log lines do not
        # land in the no-op path (they imply a real connect was attempted).
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: _DB_URL,
            }
        )
        # The disabled sink never dials `connect`, so a throwaway fake
        # stands in only to satisfy the shared runner signature.
        _, log_lines = _sync_capturing_logs(
            self,
            analytics_sync,
            _FakeConnection(),
        )
        joined = "\n".join(log_lines)
        self.assertNotIn("connecting to", joined)
        self.assertNotIn("connection established", joined)
