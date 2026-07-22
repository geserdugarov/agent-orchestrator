# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard insight-card HTML tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


class InsightsHtmlTest(unittest.TestCase):
    """The colored icon carries severity, so the rendered message
    no longer leads with a redundant `Warning.` / `Info.` prefix.
    """

    def test_message_renders_without_severity_lead_in(self) -> None:
        _, dashboard = _reload()
        banner = dashboard.InsightBanner(
            severity="warning",
            message="Agent failure rate >= 10% in this window.",
        )
        html = dashboard._insights_html([banner])
        # The message body lands verbatim (with HTML-escaping) and the
        # severity word is NOT prefixed.
        self.assertIn(
            "Agent failure rate &gt;= 10% in this window.",
            html,
        )
        self.assertNotIn("<strong>Warning.</strong>", html)
        # The CSS class still carries the severity so the colored
        # icon / banner background paints correctly.
        self.assertIn("orch-insight warning", html)
