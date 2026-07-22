# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard card extraction tests."""

import sys


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


DASHBOARD_CARDS_MODULE = "orchestrator.dashboard_cards"


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


_MOVED_CARD_MEMBERS = (
    "_card_header_html",
    "_insights_html",
    "_backend_efficiency_card_html",
    "_cost_coverage_bar_html",
    "_reliability_tiles_html",
)


class CardHtmlExtractionTest(unittest.TestCase):
    """The insight / backend-efficiency / cost-coverage / reliability-tile
    inline-HTML card family lives in `orchestrator.dashboard_cards`, and
    `orchestrator.dashboard` re-exports each builder under the same
    name so the page pipeline and the `dashboard.<name>`
    surface keep resolving to the same object.
    """

    def test_card_members_defined_in_cards_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        cards = sys.modules[DASHBOARD_CARDS_MODULE]
        for name in _MOVED_CARD_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(cards, name).__module__,
                    DASHBOARD_CARDS_MODULE,
                )

    def test_facade_reexports_cards_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        cards = sys.modules[DASHBOARD_CARDS_MODULE]
        for name in _MOVED_CARD_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(getattr(dashboard, name), getattr(cards, name))
