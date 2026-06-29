# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import _FakeConnection, _reload


class DefaultDbUrlTest(unittest.TestCase):
    """When no `db_url` kwarg is passed, `analytics.ANALYTICS_DB_URL`
    is the default."""

    def test_config_url_used_when_kwarg_omitted(self) -> None:
        analytics, analytics_read = _reload(
            {"ANALYTICS_DB_URL": "postgresql://from-env/db"}
        )
        seen: list[str] = []

        def _capture_connect(url: str) -> _FakeConnection:
            seen.append(url)
            return _FakeConnection()

        analytics_read.get_filter_options(connect=_capture_connect)
        self.assertEqual(seen[0], "postgresql://from-env/db")
        self.assertEqual(analytics.ANALYTICS_DB_URL, "postgresql://from-env/db")

    def test_explicit_kwarg_overrides_config(self) -> None:
        _, analytics_read = _reload(
            {"ANALYTICS_DB_URL": "postgresql://from-env/db"}
        )
        seen: list[str] = []

        def _capture_connect(url: str) -> _FakeConnection:
            seen.append(url)
            return _FakeConnection()

        analytics_read.get_filter_options(
            db_url="postgresql://override/db",
            connect=_capture_connect,
        )
        self.assertEqual(seen[0], "postgresql://override/db")


if __name__ == "__main__":
    unittest.main()
