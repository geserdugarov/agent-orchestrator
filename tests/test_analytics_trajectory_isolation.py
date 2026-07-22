# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics and trajectory sink isolation tests."""

import tempfile


import unittest


from datetime import datetime, timezone


from pathlib import Path


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_text as _read_text,
    write_json_lines as _write_json_lines,
    timestamp_days_ago as _ts_days_ago,
)

_TIMESTAMP_KEY = 'ts'

_PRUNE_NOW_DAY = 25
_PRUNE_NOW_HOUR = 12


DEFAULT_RETENTION_DAYS = 90


_YEAR = 2026


PRUNE_NOW = datetime(_YEAR, 5, _PRUNE_NOW_DAY, _PRUNE_NOW_HOUR, 0, 0, tzinfo=timezone.utc)


VERY_OLD_RECORD_AGE_DAYS = 200


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_ANALYTICS_RETENTION_DAYS = "ANALYTICS_RETENTION_DAYS"


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


_TRAJECTORY_RETENTION_DAYS = "TRAJECTORY_RETENTION_DAYS"


_DEFAULT_RETENTION_STR = str(DEFAULT_RETENTION_DAYS)


def _write_old_records(analytics_path: Path, trajectory_path: Path) -> None:
    old_timestamp = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW)
    _write_json_lines(analytics_path, [{_TIMESTAMP_KEY: old_timestamp, "event": "x"}])
    _write_json_lines(trajectory_path, [{_TIMESTAMP_KEY: old_timestamp, "session_id": "1"}])


class TrajectorySinkIndependenceTest(unittest.TestCase):
    """The trajectory sink is a fully independent file: its append /
    prune never open, write, or rewrite `ANALYTICS_LOG_PATH`, and it
    holds a dedicated lock so the two sinks do not serialize against one
    another.
    """

    def test_dedicated_lock_is_distinct(self) -> None:
        _, analytics = _reload()
        self.assertIsNot(analytics._FILE_LOCK, analytics._TRAJECTORY_FILE_LOCK)

    def test_append_leaves_analytics_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload(
                {
                    _ANALYTICS_LOG_PATH: str(a_path),
                    _TRAJECTORY_LOG_PATH: str(t_path),
                }
            )
            analytics.append_trajectory_record({"session_id": "s"})
            self.assertTrue(t_path.exists())
            # The analytics file was never opened by the trajectory append.
            self.assertFalse(a_path.exists())

    def test_prune_leaves_analytics_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload(
                {
                    _ANALYTICS_LOG_PATH: str(a_path),
                    _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                    _TRAJECTORY_LOG_PATH: str(t_path),
                    _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                }
            )
            # An equally-old record in BOTH files; pruning trajectory must
            # drop only the trajectory record and never rewrite analytics.
            _write_old_records(a_path, t_path)
            a_before = _read_text(a_path)
            self.assertEqual(analytics.prune_trajectory_records(now=PRUNE_NOW), 1)
            self.assertEqual(_read_text(t_path), "")
            # Analytics file is byte-for-byte unchanged.
            self.assertEqual(_read_text(a_path), a_before)

    def test_analytics_prune_ignores_trajectory(self) -> None:
        # Symmetric guard: the analytics prune must not rewrite the
        # trajectory file either.
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload(
                {
                    _ANALYTICS_LOG_PATH: str(a_path),
                    _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                    _TRAJECTORY_LOG_PATH: str(t_path),
                    _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                }
            )
            _write_old_records(a_path, t_path)
            t_before = _read_text(t_path)
            self.assertEqual(analytics.prune_old_records(now=PRUNE_NOW), 1)
            self.assertEqual(_read_text(a_path), "")
            self.assertEqual(_read_text(t_path), t_before)
