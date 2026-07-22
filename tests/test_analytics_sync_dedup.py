# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync deduplication tests."""

import unittest


from tests.analytics_sync_execution import (
    run_sync as _run_sync,
)


from tests.analytics_sync_reload import (
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
