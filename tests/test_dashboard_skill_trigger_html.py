# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-trigger HTML tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
)


ROLE_DEVELOPER = "developer"


ROLE_REVIEWER = "reviewer"


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


COLUMN_RUNS = "Runs"


ROLE_WITH_MARKUP = "dev<&>"


class SkillTriggersHtmlTest(unittest.TestCase):
    """The skill-trigger-rates aggregate table (the invocation-level
    diagnostic beneath the session-adoption matrix) is hand-rolled HTML
    (matching the backend-efficiency cards and cost-coverage bar) so the
    small, categorical per-(role, backend) table reads cleanly even when
    every rate is 0% -- the `TRACK_SKILL_TRIGGERS=off` baseline.
    """

    def test_columns_present(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 9, 3, 3)]
        html = dashboard._skill_triggers_html(rows)
        for header in ("Role", "Backend", COLUMN_RUNS, "Skill runs", "Trigger rate", "Triggers"):
            self.assertIn(f">{header}<", html)

    def test_rate_rendered_as_percent(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1, 1)]
        html = dashboard._skill_triggers_html(rows)
        # 1 of 4 runs triggered a skill -> 25%.
        self.assertIn(">25%<", html)

    def test_rate_bar_relative_to_busiest_group(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 10, 10, 10),  # rate 1.0
            self._row(ROLE_REVIEWER, BACKEND_CODEX, 10, 5, 5),  # rate 0.5
        ]
        html = dashboard._skill_triggers_html(rows)
        # Full-width bar on the 100%-rate group, half-width on the 50%.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_zero_rate_group_renders_zero_percent(self) -> None:
        # A quiet reviewer (0 skill runs) is a real signal, not a
        # dropped row: it renders as an explicit 0% with an empty bar.
        _, dashboard = _reload()
        rows = [self._row(ROLE_REVIEWER, BACKEND_CODEX, 5, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn(">0%<", html)
        self.assertIn("width:0.0%", html)

    def test_role_html_escaped(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_WITH_MARKUP, BACKEND_CLAUDE, 1, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def _row(self, role, backend, runs, skill_runs, triggers):
        return _analytics_read_module().SkillTriggerRateRow(
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=skill_runs,
            total_triggers=triggers,
        )
