# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory disabled-mode tests."""

import tempfile


import unittest


from pathlib import Path


from tests.analytics_reload_helpers import reload_analytics as _reload


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


class TrajectoryDisabledModeTest(unittest.TestCase):
    """With the trajectory sink disabled (the opt-in default), both
    `append_trajectory_record` and `prune_trajectory_records` are silent
    no-ops -- no file is ever opened and the helpers do not raise.
    """

    def test_append_creates_no_file_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, analytics = _reload()  # TRAJECTORY_LOG_PATH unset => off
            analytics.append_trajectory_record({"ts": "x", "event": "y"})
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_append_creates_no_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: "off"})
            analytics.append_trajectory_record({"ts": "x", "event": "y"})
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_prune_returns_zero_when_disabled(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: "disabled"})
        self.assertEqual(analytics.prune_trajectory_records(), 0)

    def test_prune_returns_zero_when_unset(self) -> None:
        _, analytics = _reload()
        self.assertEqual(analytics.prune_trajectory_records(), 0)
