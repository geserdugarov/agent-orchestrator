# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import _FakeConnection, _reload

_DB_URL_ENV = "ANALYTICS_DB_URL"
_ENV_URL = "postgresql://from-env/db"


class DefaultDbUrlTest(unittest.TestCase):
    """When no `db_url` kwarg is passed, `analytics.ANALYTICS_DB_URL`
    is the default."""

    def test_config_url_used_when_kwarg_omitted(self) -> None:
        analytics, analytics_read = _reload({_DB_URL_ENV: _ENV_URL})
        seen: list[str] = []
        analytics_read.get_filter_options(
            connect=lambda url: seen.append(url) or _FakeConnection(),
        )
        self.assertEqual(seen[0], _ENV_URL)
        self.assertEqual(analytics.ANALYTICS_DB_URL, _ENV_URL)

    def test_explicit_kwarg_overrides_config(self) -> None:
        _, analytics_read = _reload({_DB_URL_ENV: _ENV_URL})
        seen: list[str] = []
        analytics_read.get_filter_options(
            db_url="postgresql://override/db",
            connect=lambda url: seen.append(url) or _FakeConnection(),
        )
        self.assertEqual(seen[0], "postgresql://override/db")


if __name__ == "__main__":
    unittest.main()
