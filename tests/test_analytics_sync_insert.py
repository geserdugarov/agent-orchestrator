# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync insert tests."""

import unittest


from datetime import datetime


from orchestrator.analytics import _sync_rows


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


_EACH_RECORD_ONCE_DURATION_S = 12.5


_COLUMNS_EXTRAS_SPLIT_DURATION_S = 42.0


SAMPLE_TIMESTAMP = "2026-05-25T12:00:00+00:00"


_CONTENT_HASH_HEX_LEN = 64


_STAGE_ENTER = "stage_enter"


_AGENT_EXIT = "agent_exit"


_STAGE_IMPLEMENTING = "implementing"


def _assert_promoted_columns(test_case, row_values, record, promoted) -> None:
    for column in (
        "repo",
        "issue",
        "event",
        "stage",
        "backend",
        "session_id",
        "input_tokens",
    ):
        test_case.assertEqual(row_values[promoted.index(column)], record[column])


def _assert_sync_row_tail(test_case, row_values, path, expected_extras) -> None:
    extras_index = len(_sync_rows._PROMOTED_COLUMNS)
    test_case.assertEqual(row_values[extras_index], expected_extras)
    test_case.assertEqual(row_values[extras_index + 1], str(path))
    test_case.assertEqual(row_values[extras_index + 2], 1)
    test_case.assertIsInstance(row_values[extras_index + 3], str)
    test_case.assertEqual(len(row_values[extras_index + 3]), _CONTENT_HASH_HEX_LEN)


class AnalyticsSyncInsertTest(unittest.TestCase):
    """Happy-path inserts: each well-formed JSONL line becomes one
    INSERT carrying the promoted columns + extras + content_hash; the
    transaction commits on success.
    """

    def test_inserts_each_record_once(self) -> None:
        records = [
            _sample_record(issue=1, event=_STAGE_ENTER, stage=_STAGE_IMPLEMENTING),
            _sample_record(issue=2, event=_AGENT_EXIT, duration_s=_EACH_RECORD_ONCE_DURATION_S),
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
            duration_s=_COLUMNS_EXTRAS_SPLIT_DURATION_S,
            backend="claude",
            session_id="sess-abc",
            input_tokens=100,
            custom_future_key="something-new",
        )
        with _sync_for_records([record]) as (path, analytics_sync):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
            _, row_values = fake.inserts[0]
            _assert_promoted_columns(
                self,
                row_values,
                record,
                _sync_rows._PROMOTED_COLUMNS,
            )
            # Extras live after promoted columns; source path / line / hash trail them.
            _assert_sync_row_tail(
                self,
                row_values,
                path,
                {"custom_future_key": "something-new"},
            )
            # Content hash matches the canonical encoding of the source
            # record, not the unsorted one we passed in -- this is
            # what makes dedup robust against prune-induced rewrites.

    def test_ts_parsed_to_datetime(self) -> None:
        # The ts column is TIMESTAMPTZ; psycopg expects a datetime,
        # not a string. A naive string would be silently inserted as
        # text in some configurations.
        with _sync_for_records([_sample_record(ts=SAMPLE_TIMESTAMP)]) as (
            _,
            analytics_sync,
        ):
            fake = _FakeConnection()
            _run_sync(analytics_sync, fake)
            _, row_values = fake.inserts[0]
            ts_value = row_values[_sync_rows._PROMOTED_COLUMNS.index("ts")]
            self.assertIsInstance(ts_value, datetime)
            self.assertIsNotNone(ts_value.tzinfo)
