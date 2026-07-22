# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard sparkline SVG tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_KEYWORDS_VIA_FACADE_W = 40


_KEYWORDS_VIA_FACADE_H = 12


class SparklineSvgTest(unittest.TestCase):
    def test_accepts_historical_keywords_via_facade(self) -> None:
        # `_sparkline_svg` is re-exported through `dashboard.__all__`; its
        # historical keywords are `values`, `w`, and `h`.
        _, dashboard = _reload()
        svg = dashboard._sparkline_svg(
            values=[1.0, 2.0, 3.0], color="#111", w=_KEYWORDS_VIA_FACADE_W, h=_KEYWORDS_VIA_FACADE_H
        )
        self.assertIn('width="40"', svg)
        self.assertIn('height="12"', svg)
