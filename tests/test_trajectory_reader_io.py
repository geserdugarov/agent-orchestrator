# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory JSONL reading and log-path tests."""

import json


import tempfile


import unittest


from pathlib import Path


from unittest.mock import patch


from orchestrator import analytics


from orchestrator import trajectory_reader as tr


_BACKEND_CLAUDE = "claude"


_STAGE_IMPLEMENTING = "implementing"


_ROLE_DEVELOPER = "developer"


_TS = "2026-06-20T10:00:00+00:00"


_LOG_PATH_ATTR = "TRAJECTORY_LOG_PATH"


_READER_MODULE = "orchestrator.trajectory_reader"


_ISSUE = 42


def _write_jsonl(path: Path, lines) -> None:
    """Write `lines` (dicts -> JSON, str -> verbatim) to `path`."""
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write("{0}\n".format(line))
            else:
                fh.write("{0}\n".format(json.dumps(line)))


def _record(**overrides):
    record = {
        "ts": _TS,
        "repo": "acme/widgets",
        "issue": _ISSUE,
        "event": "agent_trajectory",
        "stage": _STAGE_IMPLEMENTING,
        "agent_role": _ROLE_DEVELOPER,
        "backend": _BACKEND_CLAUDE,
        "steps": [],
    }
    record.update(overrides)
    return record


class _ReadTrajectoriesSupport(unittest.TestCase):
    def _read_from(self, lines):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "traj.jsonl"
            _write_jsonl(path, lines)
            return tr.read_trajectories(path=path)


class ReadTrajectoryOrderTest(_ReadTrajectoriesSupport):
    def test_skips_blank_malformed_and_foreign_lines(self) -> None:
        runs = self._read_from([
            _record(issue=1),
            "",                              # blank
            "{not valid json",              # malformed
            _record(issue=2, event="agent_exit"),  # foreign
            _record(issue=3),
        ])
        self.assertEqual({run.issue for run in runs}, {1, 3})

    def test_newest_first_by_timestamp(self) -> None:
        runs = self._read_from([
            _record(issue=1, ts=_TS),
            _record(issue=2, ts="2026-06-22T10:00:00+00:00"),
            _record(issue=3, ts="2026-06-21T10:00:00+00:00"),
        ])
        self.assertEqual([run.issue for run in runs], [2, 3, 1])

    def test_equal_time_uses_file_order_newest_last(self) -> None:
        # Same second-precision ts: the record appended later (higher
        # seq) sorts first so "most recent" stays intuitive.
        runs = self._read_from([
            _record(issue=1, ts=_TS),
            _record(issue=2, ts=_TS),
        ])
        self.assertEqual([run.issue for run in runs], [2, 1])


class ReadTrajectoryPathTest(_ReadTrajectoriesSupport):
    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(
                tr.read_trajectories(path=Path(tmp_dir) / "absent.jsonl"), []
            )

    def test_disabled_sink_returns_empty(self) -> None:
        with patch.object(analytics, _LOG_PATH_ATTR, None):
            self.assertEqual(tr.read_trajectories(), [])

    def test_default_path_uses_analytics_attr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "traj.jsonl"
            _write_jsonl(path, [_record(issue=9)])
            with patch.object(analytics, _LOG_PATH_ATTR, path):
                runs = tr.read_trajectories()
        self.assertEqual([run.issue for run in runs], [9])

    def test_unreadable_file_warns_and_returns_empty(self) -> None:
        # Pointing the reader at a directory raises IsADirectoryError -- an
        # OSError that is not FileNotFoundError -- so the read takes the
        # warn-and-empty branch instead of the silent missing-file one. The
        # warning is emitted on the public `orchestrator.trajectory_reader`
        # logger even though the read pipeline lives in the private
        # `_trajectory_records` leaf, so an operator's log filter keyed on
        # that name still sees it.
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertLogs(_READER_MODULE, level="WARNING") as captured:
                runs = tr.read_trajectories(path=Path(tmp_dir))
                self.assertEqual(runs, [])
                self.assertEqual(len(captured.records), 1)
                self.assertEqual(captured.records[0].name, _READER_MODULE)
                self.assertIn("could not read trajectory log", captured.output[0])


class ResolveLogPathTest(unittest.TestCase):

    def test_unconfigured_message_when_off(self) -> None:
        with patch.object(analytics, _LOG_PATH_ATTR, None):
            self.assertIsNone(tr.resolve_log_path())
            self.assertIsNotNone(tr.log_unconfigured_message())

    def test_no_message_when_configured(self) -> None:
        with patch.object(
            analytics, _LOG_PATH_ATTR, Path("/var/log/traj.jsonl")
        ):
            self.assertEqual(
                tr.resolve_log_path(), Path("/var/log/traj.jsonl")
            )
            self.assertIsNone(tr.log_unconfigured_message())
