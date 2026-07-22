# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync row-count tests."""

import unittest


from tests.analytics_sync_reload import (
    reload_sync as _reload,
)


from tests.analytics_sync_fakes import (
    NegativeRowcountCursor as _NegativeRowcountCursor,
    RejectingBatchCursor as _RejectingBatchCursor,
)


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
            cur,
            "INSERT ...",
            batch,
            counters,
            start=float(),
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
            _RejectingBatchCursor(),
            "INSERT ...",
            [],
            counters,
            start=float(),
        )
        self.assertEqual(counters.inserted, 0)
        self.assertEqual(counters.skipped_duplicate, 0)
