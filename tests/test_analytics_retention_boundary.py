# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics retention boundary and rewrite-failure tests."""

import contextlib


import tempfile


import unittest


from datetime import datetime, timezone


from pathlib import Path


from unittest.mock import patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_text as _read_text,
    read_lines as _read_lines,
    write_json_lines as _write_json_lines,
    timestamp_days_ago as _ts_days_ago,
)


_REPO_KEY = 'repo'


_EVENT_VALUE = 'x'


_TIMESTAMP_KEY = 'ts'


_ISSUE_KEY = 'issue'


_EVENT_KEY = 'event'


_PRUNE_NOW_DAY = 25


_PRUNE_NOW_HOUR = 12


DEFAULT_RETENTION_DAYS = 90


_YEAR = 2026


PRUNE_NOW = datetime(_YEAR, 5, _PRUNE_NOW_DAY, _PRUNE_NOW_HOUR, 0, 0, tzinfo=timezone.utc)


OLD_RECORD_AGE_DAYS = 100


VERY_OLD_RECORD_AGE_DAYS = 200


ANCIENT_RECORD_AGE_DAYS = 1000


_REPO_SHORT = "o/r"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_ANALYTICS_RETENTION_DAYS = "ANALYTICS_RETENTION_DAYS"


_DEFAULT_RETENTION_STR = str(DEFAULT_RETENTION_DAYS)


@contextlib.contextmanager
def _analytics_sink(retention: str | None = None):
    """Reload the analytics package against a temporary `analytics.jsonl`
    sink, yielding `(path, analytics)`.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "analytics.jsonl"
        env = {_ANALYTICS_LOG_PATH: str(path)}
        if retention is not None:
            env[_ANALYTICS_RETENTION_DAYS] = retention
        _, analytics = _reload(env)
        yield path, analytics


class AnalyticsPruneBoundaryTest(unittest.TestCase):
    """`prune_old_records` removes records whose `ts` precedes
    `ANALYTICS_RETENTION_DAYS`, keeps newer records, no-ops when
    retention is 0 (keep forever) or the file is absent, and preserves
    malformed lines so cleanup is operator-driven.
    """

    def test_zero_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        ancient = _ts_days_ago(ANCIENT_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention="0") as (path, analytics):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: ancient, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 1, _EVENT_KEY: _EVENT_VALUE},
                ],
            )
            self.assertEqual(analytics.prune_old_records(now=now), 0)
            # File contents unchanged.
            lines = _read_lines(path)
            self.assertEqual(len(lines), 1)

    def test_negative_retention_is_no_op(self) -> None:
        # Treated identically to the documented `0 = keep forever` knob.
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention="-5") as (path, analytics):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: old_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 1, _EVENT_KEY: _EVENT_VALUE},
                ],
            )
            self.assertEqual(analytics.prune_old_records(now=now), 0)

    def test_missing_file_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics = _reload({_ANALYTICS_LOG_PATH: str(path)})
            self.assertEqual(analytics.prune_old_records(), 0)
            self.assertFalse(path.exists())

    def test_rewrite_failure_leaves_original_intact(self) -> None:
        # An OSError from the atomic rewrite (e.g. a full disk hitting
        # `os.replace`) is downgraded to a logged no-op: the prune returns
        # 0 and the original file is left untouched rather than truncated,
        # so analytics stays observability-only. The partial temp file is
        # cleaned up so no `.prune.*.tmp` orphan is left behind.
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: old_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 1, _EVENT_KEY: _EVENT_VALUE},
                ],
            )
            before = _read_text(path)
            with patch.object(
                analytics.os,
                "replace",
                side_effect=OSError("no space left on device"),
            ):
                self.assertEqual(analytics.prune_old_records(now=PRUNE_NOW), 0)
            self.assertEqual(_read_text(path), before)
            self.assertEqual(
                [entry.name for entry in path.parent.iterdir() if ".prune." in entry.name],
                [],
            )
