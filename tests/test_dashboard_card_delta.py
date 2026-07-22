# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard KPI delta-pill tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_UP_RED_ARROW_DELTA_PILL_ARGU = 0.25


_DOWN_GREEN_ARROW_DELTA_PILL = 0.25


_COLOR_NOT_ARROW_DELTA_PILL_A = 0.25


_KEYWORD_VIA_FACADE_VALUE = 0.25


class DeltaPillTest(unittest.TestCase):
    """KPI delta pills must paint cost / token increases red and
    drops green. An earlier draft mapped `invert=True && value > 0`
    to the `.down` class (green) for "Total spend" and "Total
    tokens", which painted rising cost green -- backwards for a
    cost dashboard. The fix drops `invert=True` from those KPIs so
    the default mapping (up=red, down=green) lands.
    """

    def test_positive_default_paints_up_red_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(_UP_RED_ARROW_DELTA_PILL_ARGU)
        self.assertIn("orch-delta up", html)
        self.assertIn("▲", html)

    def test_negative_default_paints_down_green_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(-_DOWN_GREEN_ARROW_DELTA_PILL)
        self.assertIn("orch-delta down", html)
        self.assertIn("▼", html)

    def test_invert_swaps_only_color_not_arrow(self) -> None:
        # `invert=True` reserved for "up is good" KPIs (issues
        # resolved, success rate). The arrow still follows the
        # value's sign so the direction is unambiguous, but the
        # color swaps so positive growth reads as green.
        _, dashboard = _reload()
        pos = dashboard._delta_pill(_COLOR_NOT_ARROW_DELTA_PILL_A, invert=True)
        neg = dashboard._delta_pill(-_COLOR_NOT_ARROW_DELTA_PILL_A, invert=True)
        self.assertIn("orch-delta down", pos)
        self.assertIn("▲", pos)
        self.assertIn("orch-delta up", neg)
        self.assertIn("▼", neg)

    def test_none_renders_nothing(self) -> None:
        # No prior window to compare against: the grey placeholder pill
        # read like a (non-functional) minimize control, so the slot is
        # dropped entirely rather than rendering a flat dash.
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(None), "")

    def test_zero_delta_renders_nothing(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(float()), "")

    def test_accepts_value_keyword_via_facade(self) -> None:
        # `_delta_pill` is re-exported through `dashboard.__all__`; `value`
        # is its historical keyword and must stay callable by name.
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(value=float()), "")
        self.assertIn("▲", dashboard._delta_pill(value=_KEYWORD_VIA_FACADE_VALUE))
