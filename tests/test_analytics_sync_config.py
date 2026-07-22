# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync configuration and disabled-path tests."""

import unittest


from tests.analytics_sync_reload import (
    reload_sync as _reload,
    reloaded_sync as _reloaded_sync,
    sync_for_records as _sync_for_records,
)

from tests.analytics_sync_fakes import (
    FakeConnection as _FakeConnection,
)

from tests.analytics_sync_payloads import (
    sample_record as _sample_record,
)


SAMPLE_TIMESTAMP = "2026-05-25T12:00:00+00:00"


_STAGE_ENTER = "stage_enter"


_ISSUE_KEY = "issue"


_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"


_DB_URL_ENV = "ANALYTICS_DB_URL"


_SENTINEL_DISABLED = "off"


_DB_URL = "postgresql://h/db"


_SYNC_MODULE = "orchestrator.analytics.sync"


_REFRESH_STMT = "REFRESH MATERIALIZED VIEW"


_LOG_FILENAME = "a.jsonl"


_ENCODING = "utf-8"


_Batch = tuple[str, list[tuple]]


class AnalyticsDbUrlConfigTest(unittest.TestCase):
    """`ANALYTICS_DB_URL` parses at import inside the analytics
    package: empty / sentinel disables; a real URL passes through
    verbatim so a libpq URL is the single-knob endpoint contract.
    """

    def test_default_is_disabled(self) -> None:
        analytics, _ = _reload()
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_empty_string_disables(self) -> None:
        analytics, _ = _reload({_DB_URL_ENV: ""})
        self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_sentinel_values_disable(self) -> None:
        for sentinel in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(value=sentinel):
                analytics, _ = _reload({_DB_URL_ENV: sentinel})
                self.assertIsNone(analytics.ANALYTICS_DB_URL)

    def test_real_url_passes_through(self) -> None:
        url = "postgresql://u:p@db.example.com:5432/orchestrator_analytics"
        analytics, _ = _reload({_DB_URL_ENV: url})
        self.assertEqual(analytics.ANALYTICS_DB_URL, url)

    def test_whitespace_stripped(self) -> None:
        analytics, _ = _reload({_DB_URL_ENV: "  postgresql://h/db  "})
        self.assertEqual(analytics.ANALYTICS_DB_URL, "postgresql://h/db")


class AnalyticsSyncDisabledTest(unittest.TestCase):
    """When either env knob is unset the sync is a silent no-op: no
    connection attempt, no row insertion, no error. Mirrors how
    `analytics.append_record` no-ops when the sink is disabled.
    """

    def test_no_op_when_db_url_unset(self) -> None:
        records = [_sample_record()]
        with _sync_for_records(records, db_url="") as (_, analytics_sync):
            connected = []
            sync_result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(sync_result.inserted, 0)
            self.assertEqual(sync_result.total_lines, 0)

    def test_no_op_when_log_path_unset(self) -> None:
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: _DB_URL,
            }
        )
        connected = []
        sync_result = analytics_sync.sync_jsonl_to_postgres(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(sync_result.inserted, 0)

    def test_no_op_when_log_file_missing(self) -> None:
        # Configured but file not created yet (orchestrator hasn't
        # emitted any record). Don't connect, don't fail. The no-op
        # writer leaves the path absent so the sync sees a missing file.
        with _reloaded_sync(lambda path: None) as (_, analytics_sync):
            connected = []
            sync_result = analytics_sync.sync_jsonl_to_postgres(
                connect=lambda url: connected.append(url) or _FakeConnection(),
            )
            self.assertEqual(connected, [])
            self.assertEqual(sync_result.inserted, 0)
