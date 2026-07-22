# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sink configuration parsing tests."""

import unittest


from pathlib import Path


from tests.analytics_reload_helpers import reload_analytics as _reload


_RETENTION_ENV_OVERRIDE_ANALYTICS_RETENTION = 30


DEFAULT_RETENTION_DAYS = 90


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_ANALYTICS_RETENTION_DAYS = "ANALYTICS_RETENTION_DAYS"


class AnalyticsConfigTest(unittest.TestCase):
    """`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` parse at import
    inside the analytics package: default-enabled under `config.LOG_DIR`,
    sentinel values disable, retention defaults to 90 days and 0 means
    keep raw data indefinitely.
    """

    def test_default_path_under_log_dir(self) -> None:
        config, analytics = _reload()
        self.assertEqual(analytics.ANALYTICS_LOG_PATH, config.LOG_DIR / "analytics.jsonl")

    def test_default_retention_is_ninety_days(self) -> None:
        _, analytics = _reload()
        self.assertEqual(
            analytics.ANALYTICS_RETENTION_DAYS,
            DEFAULT_RETENTION_DAYS,
        )

    def test_explicit_path_overrides_default(self) -> None:
        _, analytics = _reload({_ANALYTICS_LOG_PATH: "/var/log/orch/a.jsonl"})
        self.assertEqual(analytics.ANALYTICS_LOG_PATH, Path("/var/log/orch/a.jsonl"))

    def test_empty_value_disables(self) -> None:
        # Explicit empty assignment in .env is the documented disable knob.
        _, analytics = _reload({_ANALYTICS_LOG_PATH: ""})
        self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for spelling in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_ANALYTICS_LOG_PATH: spelling})
                self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_zero_retention_means_keep_forever(self) -> None:
        _, analytics = _reload({_ANALYTICS_RETENTION_DAYS: "0"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        _, analytics = _reload({_ANALYTICS_RETENTION_DAYS: "30"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, _RETENTION_ENV_OVERRIDE_ANALYTICS_RETENTION)
