# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory retention configuration tests."""

import unittest


from tests.analytics_reload_helpers import reload_analytics as _reload


DEFAULT_RETENTION_DAYS = 90


_TRAJECTORY_RETENTION_DAYS = "TRAJECTORY_RETENTION_DAYS"


class TrajectoryRetentionConfigTest(unittest.TestCase):
    """`TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` parse at import
    inside the analytics package. Unlike `ANALYTICS_LOG_PATH`, the
    trajectory sink is opt-in: an *unset* path disables it. Retention
    mirrors the analytics knob (default 90, non-positive keeps forever).
    """

    def test_default_retention_is_ninety_days(self) -> None:
        _, analytics = _reload()
        self.assertEqual(
            analytics.TRAJECTORY_RETENTION_DAYS,
            DEFAULT_RETENTION_DAYS,
        )

    def test_zero_retention_means_keep_forever(self) -> None:
        _, analytics = _reload({_TRAJECTORY_RETENTION_DAYS: "0"})
        self.assertEqual(analytics.TRAJECTORY_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        _, analytics = _reload({_TRAJECTORY_RETENTION_DAYS: "7"})
        self.assertEqual(analytics.TRAJECTORY_RETENTION_DAYS, 7)
