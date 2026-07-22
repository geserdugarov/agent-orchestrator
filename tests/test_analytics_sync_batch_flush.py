# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync batch-flush tests."""

import unittest


from unittest.mock import patch


from orchestrator.analytics import _sync_rows


from tests.analytics_sync_execution import (
    run_sync as _run_sync,
)


from tests.analytics_sync_reload import (
    sync_for_records as _sync_for_records,
    sync_for_lines as _sync_for_lines,
)


from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)


from tests.analytics_sync_payloads import (
    sample_record as _sample_record,
    sample_records as _sample_records,
    record_line as _record_line,
)


TEST_BATCH_SIZE = 3


PARTIAL_BATCH_RECORD_COUNT = TEST_BATCH_SIZE + 2


_LARGE_BATCH_SIZE = 500


_BATCH_SIZE_ATTR = "_BATCH_SIZE"


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
            _,
            analytics_sync,
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
                _sync_rows._content_hash(record)
                for record in racing_records
            )
            with patch.object(analytics_sync, _BATCH_SIZE_ATTR, len(records)):
                sync_result = _run_sync(analytics_sync, fake)
            self.assertEqual(
                sync_result.inserted,
                len(records) - len(racing_records),
            )
            self.assertEqual(
                sync_result.skipped_duplicate,
                len(racing_records),
            )
            self.assertEqual(len(fake.batches), 1)
            self.assertEqual(
                len(fake.batches[0][1]),
                len(records),
            )

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
                len(fake.batches[1][1]),
                len(records) - TEST_BATCH_SIZE,
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
