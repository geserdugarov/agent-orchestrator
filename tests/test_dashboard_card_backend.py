# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard backend-efficiency card tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_dashboard_theme as _theme_module,
)


_HEADLINE_METRICS_RENDERED_TO = 8.0


_HEADLINE_METRICS_REN_SECONDARY = 1_000_000


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


class BackendEfficiencyCardHtmlTest(unittest.TestCase):
    """The per-backend efficiency card is hand-rolled HTML so the
    caller can render one `st.markdown` per backend. Token totals
    include the cache band (`input + output + cache_read +
    cache_write`) and cache leverage is `cache_read / (cache_read +
    input)` -- the share of billable input served from cache.
    """

    def test_headline_and_metrics_rendered(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        row = self._row(
            backend=BACKEND_CLAUDE,
            runs=4,
            total_cost_usd=_HEADLINE_METRICS_RENDERED_TO,
            total_input_tokens=_HEADLINE_METRICS_REN_SECONDARY,
            total_output_tokens=0,
            total_cache_read_tokens=_HEADLINE_METRICS_REN_SECONDARY,
            total_cache_write_tokens=0,
        )
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn(BACKEND_CLAUDE, html)
        self.assertIn("4 runs", html)
        # tokens = 2M -> $8 / 2M = $4.00 / 1M tok.
        self.assertIn("$4.00 / 1M tok", html)
        # cache_read 1M / (input 1M + cache_read 1M) = 50% cache hit.
        self.assertIn("50% cache hit", html)
        # $8 / 4 runs = $2.00 / run.
        self.assertIn("$2.00 / run", html)

    def test_zero_tokens_and_runs_avoid_division(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        row = self._row(backend=BACKEND_CODEX, runs=0, total_cost_usd=float())
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn("$0.00 / 1M tok", html)
        self.assertIn("0% cache hit", html)
        self.assertIn("$0.00 / run", html)

    def test_backend_name_html_escaped(self) -> None:
        _, dashboard = _reload()
        theme = _theme_module()
        row = self._row(backend="ba<ck>", runs=1, total_cost_usd=1.0)
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn("ba&lt;ck&gt;", html)
        self.assertNotIn("ba<ck>", html)

    def _row(self, **kw):
        from orchestrator.analytics.read import BackendEfficiencyRow

        return BackendEfficiencyRow(**kw)
