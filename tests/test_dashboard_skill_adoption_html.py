# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-adoption cell and table HTML tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)

_REPO = 'owner/repo'

_CELL_VALUES_RENDERED_ROW_ARG = 41
_CELL_VALUES_RENDERED_SECONDARY = 37
_CELL_VALUES_RENDERED_INVOCAT = 122
_CELL_VALUES_RENDERED_LOAD_RO = 38


ROLE_DEVELOPER = "developer"


BACKEND_CLAUDE = "claude"


ROLE_WITH_MARKUP = "dev<&>"


class _SkillAdoptionHtmlSupport(unittest.TestCase):
    """The primary per-session skill-adoption matrix -- a hand-rolled HTML
    table over `get_skill_adoption` with one row per
    `(repo, agent_role, backend, skill)` cell. It counts skill use by
    logical agent session, so an incidental `SKILL.md` reference surfaces as
    its own diagnostic column and can never raise the adoption rate, and it
    degrades to a clear fallback notice when no session evidence exists.
    """

    def _row(self, *row_fields, **diagnostics):
        from orchestrator.analytics.read import SkillAdoptionRow

        return SkillAdoptionRow(
            repo=row_fields[0],
            skill=row_fields[1],
            agent_role=row_fields[2],
            backend=row_fields[3],
            sessions=row_fields[4],
            adopted=row_fields[5],
            invocations=diagnostics.get("invocations", row_fields[4]),
            load_rows=diagnostics.get("load_rows", 0),
            incidental=diagnostics.get("incidental", 0),
        )


class SkillAdoptionCellHtmlTest(_SkillAdoptionHtmlSupport):
    def test_columns_match_issue_spec(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(_REPO, "develop", ROLE_DEVELOPER, BACKEND_CLAUDE, 3, 2)]
        html = dashboard._skill_adoption_html(rows)
        for header in (
            "Repo",
            "Role",
            "Backend",
            "Skill",
            "Sessions",
            "Sessions using skill",
            "Adoption rate",
            "Invocation loads",
            "Incidental references",
        ):
            self.assertIn(f">{header}<", html)

    def test_cell_values_rendered(self) -> None:
        _, dashboard = _reload()
        # Distinct session denominator / numerator / diagnostics so every
        # column is exercised independently.
        rows = [
            self._row(
                _REPO,
                "develop",
                ROLE_DEVELOPER,
                BACKEND_CLAUDE,
                _CELL_VALUES_RENDERED_ROW_ARG,
                _CELL_VALUES_RENDERED_SECONDARY,
                invocations=_CELL_VALUES_RENDERED_INVOCAT,
                load_rows=_CELL_VALUES_RENDERED_LOAD_RO,
                incidental=2,
            )
        ]
        html = dashboard._skill_adoption_html(rows)
        # Full repo path (not just the trailing component) so two repos that
        # share a short name stay distinct in a cross-repo matrix.
        self.assertIn(">owner/repo<", html)
        self.assertIn(">developer<", html)
        self.assertIn(">claude<", html)
        self.assertIn(">develop<", html)
        self.assertIn('<td class="r">41</td>', html)
        self.assertIn('<td class="r">37</td>', html)
        # Adoption rate is derived from the two session counts (37/41) and
        # rounds to a whole percent.
        self.assertIn('<td class="r">90%</td>', html)
        self.assertIn('<td class="r">38</td>', html)
        self.assertIn('<td class="r">2</td>', html)

    def test_incidental_never_raises_adoption(self) -> None:
        # A purely-incidental cell -- the skill's `SKILL.md` was referenced
        # but never loaded, and no session had it available -- carries zero
        # sessions / zero adopted and an undefined (em-dash) rate, so its
        # incidental count can never be mistaken for adoption.
        _, dashboard = _reload()
        rows = [
            self._row(
                _REPO,
                "review",
                ROLE_DEVELOPER,
                BACKEND_CLAUDE,
                0,
                0,
                invocations=1,
                load_rows=0,
                incidental=1,
            )
        ]
        html = dashboard._skill_adoption_html(rows)
        # No available session -> the rate is undefined, rendered as a muted
        # em-dash rather than a misleading percentage.
        self.assertIn('<span class="orch-skilladopt-zero">—</span>', html)
        # The incidental reference is its own diagnostic column, visible as a
        # plain count while sessions / adopted stay a muted zero.
        self.assertIn('<td class="r">1</td>', html)
        self.assertIn('<span class="orch-skilladopt-zero">0</span>', html)
        # An incidental mention never produces an adoption percentage.
        self.assertNotIn("%</td>", html)
        self.assertNotIn("%</span>", html)

    def test_unadopted_available_renders_muted_zero(self) -> None:
        # A skill available to sessions that none loaded is a real "offered
        # but ignored" signal: its adoption rate renders as an explicit
        # (muted) 0% rather than the undefined em-dash.
        _, dashboard = _reload()
        rows = [
            self._row(
                _REPO,
                "review",
                ROLE_DEVELOPER,
                BACKEND_CLAUDE,
                5,
                0,
            )
        ]
        html = dashboard._skill_adoption_html(rows)
        self.assertIn('<span class="orch-skilladopt-zero">0%</span>', html)
        # The session denominator is a real count, not muted.
        self.assertIn('<td class="r">5</td>', html)


class SkillAdoptionTableHtmlTest(_SkillAdoptionHtmlSupport):
    def test_repo_role_backend_skill_html_escaped(self) -> None:
        # Every free-text cell is HTML-escaped so a skill / repo / role name
        # carrying markup cannot break out of the table.
        _, dashboard = _reload()
        rows = [self._row("o/<r&>", "sk<i>ll", ROLE_WITH_MARKUP, "back<end>", 1, 1)]
        html = dashboard._skill_adoption_html(rows)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertIn("sk&lt;i&gt;ll", html)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertIn("back&lt;end&gt;", html)
        self.assertNotIn("<r&>", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def test_empty_rows_render_fallback_not_table(self) -> None:
        # No session evidence -> a clear fallback notice renders in place of
        # the table, naming the opt-in switch so a quiet panel is not
        # mistaken for a bug.
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html([])
        self.assertIn("orch-skilladopt-empty", html)
        self.assertIn("No per-session skill adoption", html)
        self.assertIn("TRACK_SKILL_TRIGGERS", html)
        # The table markup itself is not emitted on the fallback path.
        self.assertNotIn("<table", html)

    def test_fallback_message_is_html_escaped(self) -> None:
        # The fallback message is escaped before it lands in the div, so the
        # apostrophe-carrying copy renders without breaking out.
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html([])
        self.assertIn("&#x27;", html)
