# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync malformed-input tests."""

import unittest


from datetime import timezone


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
    record_line as _record_line,
)


SAMPLE_NAIVE_TIMESTAMP = "2026-05-25T12:00:00"


_ENCODING = "utf-8"


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
        with _sync_for_lines(
            [
                '{"ts": "not-a-date", "repo": "o/r", "issue": 1, "event": "x"}',
                _record_line(),
            ]
        ) as (path, analytics_sync):
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
