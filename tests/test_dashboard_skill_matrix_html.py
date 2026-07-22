# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-matrix HTML tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ROLE_DEVELOPER = "developer"


BACKEND_CLAUDE = "claude"


COLUMN_RUNS = "Runs"


ROLE_WITH_MARKUP = "dev<&>"


class SkillMatrixHtmlTest(unittest.TestCase):
    """The per-skill trigger matrix is the second table in the skill
    panel's invocation-level diagnostics expander -- a hand-rolled HTML
    table over `get_skill_trigger_matrix` with one row per
    `(repo, agent_role, backend, skill)` cell. It folds each repo's
    skill catalog into the observed triggers so an offered-but-never-
    triggered skill renders as an explicit `0` cell, and degrades to a
    clear fallback notice when no catalog-backed matrix can be built.
    """

    def test_columns_match_issue_spec(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("owner/repo", "develop", ROLE_DEVELOPER, BACKEND_CLAUDE, 2)]
        html = dashboard._skill_matrix_html(rows)
        for header in ("Repo", "Role", "Backend", "Skill", COLUMN_RUNS, "Runs with skill", "Trigger rate"):
            self.assertIn(f">{header}<", html)

    def test_cell_values_rendered(self) -> None:
        _, dashboard = _reload()
        # Distinct cohort total (Runs) and trigger count (Runs with skill)
        # so both columns are exercised independently.
        rows = [
            self._row(
                "owner/repo",
                "develop",
                ROLE_DEVELOPER,
                BACKEND_CLAUDE,
                5,
                skill_runs=3,
            )
        ]
        html = dashboard._skill_matrix_html(rows)
        # Full repo path (not just the trailing component) so two repos
        # that share a short name stay distinct in a cross-repo matrix.
        self.assertIn(">owner/repo<", html)
        self.assertIn(">developer<", html)
        self.assertIn(">claude<", html)
        self.assertIn(">develop<", html)
        self.assertIn(">5<", html)
        self.assertIn(">3<", html)
        # Trigger rate is derived from the two counts (3/5) and rounds to
        # a whole percent, matching the aggregate table's format.
        self.assertIn(">60%<", html)

    def test_zero_count_renders_muted_zero(self) -> None:
        # An offered-but-never-triggered catalog cell is a real
        # "offered but quiet" signal, not a dropped row: its "Runs with
        # skill" renders as an explicit (muted) 0 rather than going
        # missing, while the cohort `Runs` total stays a plain number.
        _, dashboard = _reload()
        rows = [
            self._row(
                "owner/repo",
                "review",
                ROLE_DEVELOPER,
                BACKEND_CLAUDE,
                4,
                skill_runs=0,
            )
        ]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("orch-skillmatrix-zero", html)
        self.assertIn(">0<", html)
        # The derived trigger rate is `0%` and muted on the same signal,
        # so an offered-but-quiet cell reads consistently across columns.
        self.assertIn('<span class="orch-skillmatrix-zero">0%</span>', html)
        # The cohort total is not muted -- it is a plain right-aligned cell.
        self.assertIn('<td class="r">4</td>', html)

    def test_repo_role_backend_skill_html_escaped(self) -> None:
        # Every free-text cell is HTML-escaped so a skill / repo / role
        # name carrying markup cannot break out of the table.
        _, dashboard = _reload()
        rows = [self._row("o/<r&>", "sk<i>ll", ROLE_WITH_MARKUP, "back<end>", 1)]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertIn("sk&lt;i&gt;ll", html)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertIn("back&lt;end&gt;", html)
        self.assertNotIn("<r&>", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def test_empty_rows_render_fallback_not_table(self) -> None:
        # No catalog records matched and no run fired a skill -> there
        # is no catalog-backed matrix to build, so a clear fallback
        # notice renders in place of the table.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("orch-skillmatrix-empty", html)
        self.assertIn("No catalog-backed skill matrix", html)
        # Names the opt-in switch so a quiet panel is not mistaken for a
        # bug, mirroring the aggregate table's caption.
        self.assertIn("TRACK_SKILL_TRIGGERS", html)
        # The table markup itself is not emitted on the fallback path.
        self.assertNotIn("<table", html)

    def test_fallback_message_is_html_escaped(self) -> None:
        # The fallback message is escaped before it lands in the div, so
        # the apostrophe-carrying copy renders without breaking out.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("&#x27;", html)

    def _row(self, *row_fields, **counts):
        from orchestrator.analytics.read import SkillTriggerMatrixRow

        return SkillTriggerMatrixRow(
            repo=row_fields[0],
            skill=row_fields[1],
            agent_role=row_fields[2],
            backend=row_fields[3],
            runs=row_fields[4],
            skill_runs=counts.get("skill_runs", row_fields[4]),
        )
