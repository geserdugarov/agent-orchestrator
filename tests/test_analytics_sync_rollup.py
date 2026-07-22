# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics daily-rollup refresh tests."""

import unittest


from orchestrator.analytics import _sync_rows

from tests.analytics_sync_execution import (
    run_sync as _run_sync,
    sync_capturing_logs as _sync_capturing_logs,
    refresh_sqls as _refresh_sqls,
)

from tests.analytics_sync_reload import (
    reload_sync as _reload,
    sync_for_records as _sync_for_records,
    sync_for_lines as _sync_for_lines,
)

from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)

from tests.analytics_sync_payloads import (
    sample_record as _sample_record,
    sample_records as _sample_records,
)


SAMPLE_TIMESTAMP = "2026-05-25T12:00:00+00:00"


_STAGE_ENTER = "stage_enter"


_ISSUE_KEY = "issue"


_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"


_DB_URL_ENV = "ANALYTICS_DB_URL"


_SENTINEL_DISABLED = "off"


_DB_URL = "postgresql://h/db"


_SYNC_MODULE = "orchestrator.analytics.sync"


_LOG_LEVEL_INFO = "INFO"


_REFRESH_STMT = "REFRESH MATERIALIZED VIEW"


_LOG_FILENAME = "a.jsonl"


_ENCODING = "utf-8"


_Batch = tuple[str, list[tuple]]


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
                self,
                analytics_sync,
                fake,
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
            fake.seen_hashes.update(_sync_rows._content_hash(rec) for rec in records)
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
            fake.raise_on_refresh = RuntimeError("materialized view does not exist")
            sync_result, log_lines = _sync_capturing_logs(
                self,
                analytics_sync,
                fake,
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
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: _DB_URL,
            }
        )
        connected: list[str] = []
        analytics_sync.sync_jsonl_to_postgres(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
