# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard helper compatibility tests."""

import inspect


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


ENTRYPOINT_ATTR = "main"


class _MainSourceTest(unittest.TestCase):
    """Base for source checks over the lazy entrypoint and page helpers.

    Streamlit / Plotly are opt-in (not installed for the default
    `uv sync --locked`), so these read the rendered function source
    rather than driving the page under Streamlit. The entrypoint loads
    optional modules lazily and the page pipeline delegates controls,
    read waves, empty states, and widget sections to named helpers, so
    `_source_of` fetches the boundary each assertion protects.
    """

    def _main_source(self) -> str:
        return self._source_of(ENTRYPOINT_ATTR)

    def _source_of(self, name: str) -> str:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        return inspect.getsource(getattr(dashboard, name))


class DashboardCompatibilityHelperTest(_MainSourceTest):
    """Exported dashboard helpers retain their historical call shapes."""

    def test_topbar_signature_is_stable(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertEqual(
            tuple(inspect.signature(dashboard._topbar_html).parameters),
            (
                "extent",
                "distinct_repos",
                "total_events",
                "spend_in_range",
                "fmt_money_exact",
                "fmt_num",
            ),
        )

    def test_drilldown_signature_and_delegate_stable(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertEqual(
            tuple(inspect.signature(dashboard._render_drilldown).parameters),
            (
                "st",
                "pd",
                "window",
                "repo_filter",
                "issue_input_parsed",
                "event_filter",
                "stage_filter",
            ),
        )
        self.assertIn(
            "_render_drilldown_view(modules, filters)",
            self._source_of("_render_drilldown"),
        )
