# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plotly-free theme tokens in `orchestrator.dashboard_theme`.

These tests do NOT require plotly: the theme module is intentionally
dependency-free so the dashboard chrome can pull semantic colors
without forcing the optional `dashboard` group on every caller. The
chart-builder tests live in `tests/test_dashboard_charts.py` and skip
cleanly when plotly is absent.
"""
from __future__ import annotations

import unittest

from orchestrator import dashboard_theme as theme


class ColorForTest(unittest.TestCase):

    def test_explicit_palette_wins(self) -> None:
        # An explicit mapping always overrides domain-position lookup,
        # so the stage colors stay stable even if the chart re-orders
        # rows.
        self.assertEqual(
            theme.color_for(
                "implementing",
                ["implementing", "validating"],
                explicit=theme.STAGE_COLORS,
            ),
            theme.STAGE_COLORS["implementing"],
        )

    def test_domain_position_drives_color(self) -> None:
        # Without an explicit mapping, the n-th entry of the domain
        # gets the n-th entry of `CATEGORICAL_PALETTE`. That property
        # is what makes "the same domain in the same order" produce
        # the same colors across chart re-renders.
        self.assertEqual(
            theme.color_for("a", ["a", "b", "c"]),
            theme.CATEGORICAL_PALETTE[0],
        )
        self.assertEqual(
            theme.color_for("b", ["a", "b", "c"]),
            theme.CATEGORICAL_PALETTE[1],
        )

    def test_unknown_key_in_domain_falls_through_to_hash(self) -> None:
        # `domain` is provided but the key is not in it -- the helper
        # should still return a palette color rather than raising.
        color = theme.color_for("zzz", ["a", "b"])
        self.assertIn(color, theme.CATEGORICAL_PALETTE)

    def test_no_domain_returns_palette_color(self) -> None:
        self.assertIn(
            theme.color_for("anything"), theme.CATEGORICAL_PALETTE
        )


class BaseLayoutTest(unittest.TestCase):

    def test_returns_plain_dict(self) -> None:
        # The layout must be JSON-friendly plain data; chart builders
        # splat it into `fig.update_layout(**...)`, so any non-dict
        # type would break the call site.
        layout = theme.base_layout()
        self.assertIsInstance(layout, dict)
        self.assertEqual(layout["paper_bgcolor"], theme.BACKGROUND)
        self.assertEqual(layout["plot_bgcolor"], theme.BACKGROUND)
        self.assertIn("font", layout)
        self.assertNotIn("title", layout)

    def test_title_threads_through(self) -> None:
        layout = theme.base_layout(title="Hello")
        self.assertEqual(layout["title"]["text"], "Hello")
        # The top margin grows when a title is present so the title
        # has room to render above the plot area.
        self.assertGreaterEqual(layout["margin"]["t"], 32)


class PaletteContractTest(unittest.TestCase):
    """The categorical palettes are public contracts -- every value
    referenced by the chart builders must be present and resolve to a
    hex color string. Regressions here would silently break legend
    coloring on a live dashboard.
    """

    def test_event_colors_cover_known_events(self) -> None:
        for event in ("stage_enter", "stage_evaluation", "agent_exit"):
            self.assertIn(event, theme.EVENT_COLORS)
            self.assertTrue(theme.EVENT_COLORS[event].startswith("#"))

    def test_stage_colors_cover_workflow_labels(self) -> None:
        expected = {
            "decomposing", "blocked", "ready", "umbrella",
            "implementing", "validating", "documenting", "in_review",
            "fixing", "resolving_conflict", "question", "done",
            "rejected",
        }
        self.assertEqual(set(theme.STAGE_COLORS).intersection(expected),
                         expected)

    def test_cost_source_colors_cover_usage_tags(self) -> None:
        for tag in ("reported", "estimated", "unknown-price", "no-usage"):
            self.assertIn(tag, theme.COST_SOURCE_COLORS)


if __name__ == "__main__":
    unittest.main()
