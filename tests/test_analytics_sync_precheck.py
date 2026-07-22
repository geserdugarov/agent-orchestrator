# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync duplicate pre-check tests."""

import unittest


from orchestrator.analytics import _sync_rows


from tests.analytics_sync_execution import (
    run_sync as _run_sync,
    select_sqls as _select_sqls,
)


from tests.analytics_sync_reload import (
    sync_for_records as _sync_for_records,
)


from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)


from tests.analytics_sync_payloads import (
    sample_record as _sample_record,
    sample_records as _sample_records,
)


TEST_BATCH_SIZE = 3


_STAGE_ENTER = "stage_enter"


_AGENT_EXIT = "agent_exit"


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
            _,
            analytics_sync,
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
                _sync_rows._content_hash(record)
                for record in records[:-1]
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
