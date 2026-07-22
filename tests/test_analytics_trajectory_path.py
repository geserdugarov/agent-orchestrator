# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory sink path configuration tests."""

import unittest


from pathlib import Path


from tests.analytics_reload_helpers import reload_analytics as _reload


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


_TRAJECTORY_RETENTION_DAYS = "TRAJECTORY_RETENTION_DAYS"


class TrajectoryPathConfigTest(unittest.TestCase):
    """`TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` parse at import
    inside the analytics package. Unlike `ANALYTICS_LOG_PATH`, the
    trajectory sink is opt-in: an *unset* path disables it. Retention
    mirrors the analytics knob (default 90, non-positive keeps forever).
    """

    def test_unset_disables(self) -> None:
        # The opt-in distinction from analytics: no env var => off.
        _, analytics = _reload()
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_empty_value_disables(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: ""})
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for spelling in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRAJECTORY_LOG_PATH: spelling})
                self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_explicit_path_enables(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: "/var/log/orch/t.jsonl"})
        self.assertEqual(analytics.TRAJECTORY_LOG_PATH, Path("/var/log/orch/t.jsonl"))

    def test_knobs_exported(self) -> None:
        _, analytics = _reload()
        self.assertIn(_TRAJECTORY_LOG_PATH, analytics.__all__)
        self.assertIn(_TRAJECTORY_RETENTION_DAYS, analytics.__all__)
        self.assertIn("append_trajectory_record", analytics.__all__)
        self.assertIn("prune_trajectory_records", analytics.__all__)
