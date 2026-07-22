# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard cost-coverage card tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_dashboard_theme as _theme_module,
)


_SIZED_TOKEN_SHARE_ROW_ARGUME = 750


_SIZED_TOKEN_SHARE_RO_SECONDARY = 250


COST_SOURCE_REPORTED = "reported"


COST_SOURCE_UNKNOWN_PRICE = "unknown-price"


class CostCoverageBarHtmlTest(unittest.TestCase):
    """The cost-attribution coverage bar sizes segments by token share
    when the window carries token volume, falling back to run share
    only when it does not -- a few high-token runs can dominate cost
    while looking like a thin slice of the run count.
    """

    def test_segments_sized_by_token_share(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        # 750 / 1000 tokens = 75% by tokens, NOT 10% by run count.
        rows = [
            self._row(COST_SOURCE_REPORTED, 1, _SIZED_TOKEN_SHARE_ROW_ARGUME),
            self._row(COST_SOURCE_UNKNOWN_PRICE, 9, _SIZED_TOKEN_SHARE_RO_SECONDARY),
        ]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("Cost attribution coverage", html)
        self.assertIn("width:75.0%", html)
        self.assertIn("25.0%", html)

    def test_falls_back_to_run_share_without_tokens(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        # No token volume yet -> size by run share: 3 / 4 = 75%.
        rows = [
            self._row(COST_SOURCE_REPORTED, 3, 0),
            self._row("unknown", 1, 0),
        ]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("width:75.0%", html)

    def test_cost_source_html_escaped(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        rows = [self._row("src<&>", 1, 10)]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("src&lt;&amp;&gt;", html)
        self.assertNotIn("src<&>", html)

    def _row(self, source, runs, tokens):
        from orchestrator.analytics.read import CostCoverageRow

        return CostCoverageRow(cost_source=source, runs=runs, total_tokens=tokens)
