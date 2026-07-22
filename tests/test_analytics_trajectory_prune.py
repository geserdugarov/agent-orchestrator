# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory retention selection and boundary tests."""

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

_TIMESTAMP_KEY = 'ts'
_ONE_TEXT = '1'
_SESSION_ID_KEY = 'session_id'

_PRUNE_NOW_DAY = 25
_PRUNE_NOW_HOUR = 12
_PATH = 5000


DEFAULT_RETENTION_DAYS = 90


_YEAR = 2026


PRUNE_NOW = datetime(_YEAR, 5, _PRUNE_NOW_DAY, _PRUNE_NOW_HOUR, 0, 0, tzinfo=timezone.utc)


FRESH_RECORD_AGE_DAYS = 1


RECENT_RECORD_AGE_DAYS = 10


OLD_RECORD_AGE_DAYS = 100


VERY_OLD_RECORD_AGE_DAYS = 200


ANCIENT_RECORD_AGE_DAYS = 1000


_ENCODING = "utf-8"


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


_TRAJECTORY_RETENTION_DAYS = "TRAJECTORY_RETENTION_DAYS"


_DEFAULT_RETENTION_STR = str(DEFAULT_RETENTION_DAYS)


def _logged_call(test_case, logger, action):
    with contextlib.ExitStack() as cleanup:
        captured = cleanup.enter_context(
            test_case.assertLogs(logger, level="WARNING"),
        )
        call_result = action()
    return call_result, list(captured.output)


@contextlib.contextmanager
def _trajectory_sink(retention: str | None = None):
    """Reload the analytics package against a temporary `trajectory.jsonl`
    sink, yielding `(path, analytics)`.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trajectory.jsonl"
        env = {_TRAJECTORY_LOG_PATH: str(path)}
        if retention is not None:
            env[_TRAJECTORY_RETENTION_DAYS] = retention
        _, analytics = _reload(env)
        yield path, analytics


class TrajectoryPruneSelectionTest(unittest.TestCase):
    """`prune_trajectory_records` mirrors `prune_old_records`: removes
    records past `TRAJECTORY_RETENTION_DAYS`, no-ops at retention <= 0 or
    on an absent file, and preserves malformed / unparseable lines.
    """

    def test_removes_old_records_keeps_recent(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        new_ts = _ts_days_ago(RECENT_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(
                path,
                [
                    {_TIMESTAMP_KEY: old_ts, _SESSION_ID_KEY: _ONE_TEXT},
                    {_TIMESTAMP_KEY: new_ts, _SESSION_ID_KEY: "2"},
                    {_TIMESTAMP_KEY: old_ts, _SESSION_ID_KEY: "3"},
                ],
            )
            self.assertEqual(analytics.prune_trajectory_records(now=now), 2)
            self.assertEqual(
                [json.loads(line)[_SESSION_ID_KEY] for line in _read_lines(path)],
                ["2"],
            )

    def test_no_records_old_enough_does_not_rewrite(self) -> None:
        now = PRUNE_NOW
        new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(path, [{_TIMESTAMP_KEY: new_ts, _SESSION_ID_KEY: _ONE_TEXT}])
            mtime_before = path.stat().st_mtime_ns
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_malformed_lines_preserved(self) -> None:
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding=_ENCODING) as fh:
                fh.write("this is not json\n")
                fh.write(f"{json.dumps({_TIMESTAMP_KEY: old_ts, _SESSION_ID_KEY: _ONE_TEXT})}\n")
                fh.write('{"ts": "not-a-date", "session_id": "2"}\n')
                fh.write('{"session_id": "no-ts-field"}\n')
            self.assertEqual(analytics.prune_trajectory_records(now=PRUNE_NOW), 1)
            kept = _read_lines(path)
            self.assertEqual(len(kept), 3)
            self.assertIn("this is not json", kept[0])

    def test_naive_timestamp_treated_as_utc(self) -> None:
        now = PRUNE_NOW
        old_naive = (
            (now - timedelta(days=OLD_RECORD_AGE_DAYS))
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path,
            analytics,
        ):
            _write_json_lines(path, [{_TIMESTAMP_KEY: old_naive, _SESSION_ID_KEY: _ONE_TEXT}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 1)
            self.assertEqual(_read_text(path), "")


class TrajectoryPruneBoundaryTest(unittest.TestCase):
    """`prune_trajectory_records` mirrors `prune_old_records`: removes
    records past `TRAJECTORY_RETENTION_DAYS`, no-ops at retention <= 0 or
    on an absent file, and preserves malformed / unparseable lines.
    """

    def test_zero_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        ancient = _ts_days_ago(ANCIENT_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention="0") as (path, analytics):
            _write_json_lines(path, [{_TIMESTAMP_KEY: ancient, _SESSION_ID_KEY: _ONE_TEXT}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)
            self.assertEqual(len(_read_lines(path)), 1)

    def test_negative_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention="-5") as (path, analytics):
            _write_json_lines(path, [{_TIMESTAMP_KEY: old_ts, _SESSION_ID_KEY: _ONE_TEXT}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)

    def test_missing_file_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: str(path)})
            self.assertEqual(analytics.prune_trajectory_records(), 0)
            self.assertFalse(path.exists())

    def test_probe_oserror_becomes_warning(self) -> None:
        # `Path.exists()` re-raises OSErrors that don't mean "absent"
        # (e.g. ENAMETOOLONG on an over-long path). That probe runs
        # before the read/rewrite try-block, so without its own guard
        # the error would escape the per-tick caller. The prune must
        # warn and no-op (return 0) instead of raising.
        with tempfile.TemporaryDirectory() as td:
            # A single path component well past NAME_MAX (255) makes the
            # underlying stat() raise OSError [Errno 36] File name too long.
            path = Path(td) / ("x" * _PATH)
            _, analytics = _reload(
                {
                    _TRAJECTORY_LOG_PATH: str(path),
                    _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                }
            )
            removed, log_output = _logged_call(
                self,
                analytics.log,
                analytics.prune_trajectory_records,
            )
            self.assertEqual(removed, 0)
            self.assertTrue(any("prune" in message for message in log_output))
