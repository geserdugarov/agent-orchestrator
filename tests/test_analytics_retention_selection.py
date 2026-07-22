# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics retention selection tests."""

import contextlib


import json


import tempfile


import unittest


from datetime import datetime, timedelta, timezone


from pathlib import Path


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


FRESH_RECORD_AGE_DAYS = 1


RECENT_RECORD_AGE_DAYS = 10


OLD_RECORD_AGE_DAYS = 100


VERY_OLD_RECORD_AGE_DAYS = 200


_REPO_SHORT = "o/r"


_ENCODING = "utf-8"


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


class AnalyticsPruneSelectionTest(unittest.TestCase):
    """`prune_old_records` removes records whose `ts` precedes
    `ANALYTICS_RETENTION_DAYS`, keeps newer records, no-ops when
    retention is 0 (keep forever) or the file is absent, and preserves
    malformed lines so cleanup is operator-driven.
    """

    def test_removes_old_records_keeps_recent(self) -> None:
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=PRUNE_NOW)
        new_ts = _ts_days_ago(RECENT_RECORD_AGE_DAYS, now=PRUNE_NOW)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: old_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 1, _EVENT_KEY: _EVENT_VALUE},
                    {_TIMESTAMP_KEY: new_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 2, _EVENT_KEY: "y"},
                    {_TIMESTAMP_KEY: old_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 3, _EVENT_KEY: "z"},
                ],
            )
            self.assertEqual(analytics.prune_old_records(now=PRUNE_NOW), 2)
            remaining = [json.loads(line) for line in _read_lines(path)]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0][_ISSUE_KEY], 2)

    def test_no_records_old_enough_does_not_rewrite(self) -> None:
        now = PRUNE_NOW
        new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: new_ts, _REPO_KEY: _REPO_SHORT, _ISSUE_KEY: 1, _EVENT_KEY: _EVENT_VALUE},
                ],
            )
            mtime_before = path.stat().st_mtime_ns
            self.assertEqual(analytics.prune_old_records(now=now), 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_malformed_lines_preserved(self) -> None:
        # Non-JSON lines, JSON without `ts`, and unparseable `ts` strings
        # survive the prune so operators can clean up rather than having
        # the helper silently drop data it cannot interpret.
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding=_ENCODING) as fh:
                fh.write("this is not json\n")
                fh.write(
                    json.dumps(
                        {
                            _TIMESTAMP_KEY: old_ts,
                            _REPO_KEY: _REPO_SHORT,
                            _ISSUE_KEY: 1,
                            _EVENT_KEY: _EVENT_VALUE,
                        }
                    )
                    + "\n"
                )
                fh.write('{"ts": "not-a-date", "event": "y"}\n')
                fh.write('{"event": "no-ts-field"}\n')
            # Only the parseable old record is removed; the three other
            # malformed-or-missing-ts lines survive.
            self.assertEqual(analytics.prune_old_records(now=PRUNE_NOW), 1)
            kept = _read_lines(path)
            self.assertEqual(len(kept), 3)
            self.assertIn("this is not json", kept[0])

    def test_naive_timestamp_treated_as_utc(self) -> None:
        # Pre-existing records written without tz info (or by an older
        # writer) must still be comparable; treat them as UTC rather than
        # raising and aborting the prune.
        now = PRUNE_NOW
        old_naive = (
            (now - timedelta(days=OLD_RECORD_AGE_DAYS))
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(
                path,
                [
                    {
                        _TIMESTAMP_KEY: old_naive,
                        _REPO_KEY: _REPO_SHORT,
                        _ISSUE_KEY: 1,
                        _EVENT_KEY: _EVENT_VALUE,
                    }
                ],
            )
            self.assertEqual(analytics.prune_old_records(now=now), 1)
            self.assertEqual(_read_text(path), "")
