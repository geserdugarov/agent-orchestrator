# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync transaction tests."""

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
    sample_record as _sample_record,
)


class AnalyticsSyncTransactionTest(unittest.TestCase):
    """A driver-side error mid-stream rolls the transaction back so
    a partial batch is never committed. The exception propagates so
    the CLI surfaces a non-zero exit code rather than reporting
    "success" on a half-inserted batch.
    """

    def test_execute_error_rolls_back_and_propagates(self) -> None:
        with _sync_for_records([_sample_record()]) as (_, analytics_sync):
            fake = _FakeConnection()
            fake.raise_on_executemany = RuntimeError("simulated driver failure")
            with self.assertRaises(RuntimeError):
                _run_sync(analytics_sync, fake)
            self.assertEqual(fake.commit_called, 0)
            self.assertEqual(fake.rollback_called, 1)
            self.assertEqual(fake.close_called, 1)
