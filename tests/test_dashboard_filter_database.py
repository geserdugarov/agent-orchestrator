# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard database-configuration message tests."""

import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


class DbUnconfiguredMessageTest(unittest.TestCase):
    def test_unset_url_returns_message(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(
            dashboard.db_unconfigured_message(),
            dashboard.UNCONFIGURED_DB_MESSAGE,
        )

    def test_disable_sentinel_returns_message(self) -> None:
        # Each of the documented disable sentinels collapses to None
        # inside `config`, so the helper should treat them the same.
        for sentinel in ("off", "disabled", "none", "OFF", "Disabled"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload({ANALYTICS_DB_URL_ENV: sentinel})
                self.assertEqual(
                    dashboard.db_unconfigured_message(),
                    dashboard.UNCONFIGURED_DB_MESSAGE,
                )

    def test_configured_url_returns_none(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertIsNone(dashboard.db_unconfigured_message())
