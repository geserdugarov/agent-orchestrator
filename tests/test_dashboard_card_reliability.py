# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard reliability-card tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


FULL_WINDOW_AGENT_RUNS = 250


FULL_WINDOW_TIMEOUTS = 17


KPI_AGENT_RUNS = "Agent runs"


KPI_TIMEOUTS = "Timeouts"


class ReliabilityTilesHtmlTest(unittest.TestCase):
    """The reliability strip renders the `(value, label, tone)` triples
    from `reliability_tile_data`; numeric values format through
    `fmt_num`, string values pass through verbatim, and the `tone`
    class paints the warn / bad tiles.
    """

    def test_tiles_carry_value_label_and_tone(self) -> None:
        _, dashboard = _reload()
        tiles = [
            (FULL_WINDOW_AGENT_RUNS, KPI_AGENT_RUNS, ""),
            ("0%", "Success rate", "bad"),
            (FULL_WINDOW_TIMEOUTS, KPI_TIMEOUTS, "bad"),
        ]
        html = dashboard._reliability_tiles_html(tiles, fmt_num=lambda count: f"{count}")
        self.assertIn("orch-rel-tiles", html)
        self.assertIn(f">{FULL_WINDOW_AGENT_RUNS}<", html)
        self.assertIn(">0%<", html)  # string value passes through
        self.assertIn(">Timeouts<", html)
        self.assertIn("orch-rel-tile bad", html)

    def test_label_html_escaped(self) -> None:
        _, dashboard = _reload()
        tiles = [(1, "la<b>el", "")]
        html = dashboard._reliability_tiles_html(tiles, fmt_num=str)
        self.assertIn("la&lt;b&gt;el", html)
        self.assertNotIn("la<b>el", html)
