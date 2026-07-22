# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-adoption header and row sorting tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ROLE_DEVELOPER = "developer"


ROLE_REVIEWER = "reviewer"


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


REPO_C_CELL_FRAGMENT = ">c/repo<"


SORT_ASC = "asc"


SORT_DESC = "desc"


ADOPT_SORT_PARAM = "adopt_sort"


ADOPT_DIR_PARAM = "adopt_dir"


ADOPT_SORT_KEYS = (
    "repo",
    "role",
    "backend",
    "skill",
    "sessions",
    "adopted",
    "rate",
    "loads",
    "incidental",
)


SORT_KEY_SESSIONS = "sessions"


class _SkillAdoptionSortSupport(unittest.TestCase):
    """The per-session adoption matrix column headers are clickable sort
    controls: each is an anchor writing `adopt_sort` / `adopt_dir` query
    params, and the caller feeds the parsed `(column, direction)` back into
    `_skill_adoption_html` so the rows re-sort on that column and the active
    header shows a ▲ / ▼ indicator.
    """

    def _row(self, *row_fields):
        from orchestrator.analytics.read import SkillAdoptionRow

        return SkillAdoptionRow(
            repo=row_fields[0],
            skill=row_fields[1],
            agent_role=row_fields[2],
            backend=row_fields[3],
            sessions=row_fields[4],
            adopted=row_fields[5],
            invocations=row_fields[4],
        )

    def _rows(self):
        # Distinct repo / session values per row so an ordering assertion can
        # key off either without ambiguity.
        return [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 2, 1),
            self._row("a/repo", "beta", ROLE_REVIEWER, BACKEND_CODEX, 9, 9),
            self._row("c/repo", "gamma", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, 0),
        ]


class SkillAdoptionHeaderSortTest(_SkillAdoptionSortSupport):
    def test_headers_link_to_self_for_sorting(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(self._rows())
        # Every column is an in-tab anchor pointing at its own sort param.
        for key in ADOPT_SORT_KEYS:
            self.assertIn(f"?{ADOPT_SORT_PARAM}={key}&{ADOPT_DIR_PARAM}=", html)
        self.assertIn('target="_self"', html)
        # Text columns default a first click to ascending, numeric ones to
        # descending (largest first is the interesting end for counts).
        self.assertIn(f"?{ADOPT_SORT_PARAM}=repo&{ADOPT_DIR_PARAM}={SORT_ASC}", html)
        self.assertIn(f"?{ADOPT_SORT_PARAM}=sessions&{ADOPT_DIR_PARAM}={SORT_DESC}", html)
        # With no active sort no header carries a direction indicator.
        self.assertNotIn('<span class="orch-skilladopt-sort">', html)

    def test_descending_shows_down_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(
            self._rows(),
            sort_key=SORT_KEY_SESSIONS,
            descending=True,
        )
        # Exactly one column is marked active, and it shows the ▼ arrow.
        self.assertEqual(
            html.count('<span class="orch-skilladopt-sort">'),
            1,
        )
        self.assertIn(
            '<span class="orch-skilladopt-sort">▼</span>',
            html,
        )
        # Re-clicking the active (descending) column flips it to ascending.
        self.assertIn(f"?{ADOPT_SORT_PARAM}=sessions&{ADOPT_DIR_PARAM}={SORT_ASC}", html)

    def test_ascending_shows_up_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(
            self._rows(),
            sort_key="repo",
            descending=False,
        )
        self.assertIn(
            '<span class="orch-skilladopt-sort">▲</span>',
            html,
        )
        self.assertIn(f"?{ADOPT_SORT_PARAM}=repo&{ADOPT_DIR_PARAM}={SORT_DESC}", html)


class SkillAdoptionRowSortTest(_SkillAdoptionSortSupport):
    def test_rows_render_in_selected_column_order(self) -> None:
        _, dashboard = _reload()
        asc = dashboard._skill_adoption_html(
            self._rows(),
            sort_key=SORT_KEY_SESSIONS,
            descending=False,
        )
        # sessions 2 < 5 < 9 -> repos b, c, a in that order.
        self.assertLess(asc.index(">b/repo<"), asc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(asc.index(REPO_C_CELL_FRAGMENT), asc.index(">a/repo<"))
        desc = dashboard._skill_adoption_html(
            self._rows(),
            sort_key=SORT_KEY_SESSIONS,
            descending=True,
        )
        self.assertLess(desc.index(">a/repo<"), desc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(desc.index(REPO_C_CELL_FRAGMENT), desc.index(">b/repo<"))

    def test_unsorted_defaults_repo_asc_rate_desc(self) -> None:
        # No sort key -> the default view orders rows by repo ascending, then
        # adoption rate descending within each repo, so each repo's
        # most-adopted skills lead. Two rows share a repo with different
        # rates so both keys are exercised (skills identify the rows).
        _, dashboard = _reload()
        rows = [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "beta", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "gamma", ROLE_REVIEWER, BACKEND_CODEX, 4, 3),
        ]
        html = dashboard._skill_adoption_html(rows)
        # Within a/repo, rate descending: gamma (75%) precedes beta (25%).
        self.assertLess(
            html.index(">gamma<"),
            html.index(">beta<"),
        )
        # Repo ascending: the a/repo rows precede the b/repo row.
        self.assertLess(
            html.index(">beta<"),
            html.index(">alpha<"),
        )

    def test_sort_helper_unknown_key_is_identity(self) -> None:
        from orchestrator import dashboard_skill_adoption

        rows = self._rows()
        sorted_rows = dashboard_skill_adoption._sort_skill_adoption_rows(
            rows,
            None,
            False,
        )
        self.assertEqual(sorted_rows, rows)
        sorted_rows = dashboard_skill_adoption._sort_skill_adoption_rows(
            rows,
            "bogus",
            True,
        )
        self.assertEqual(sorted_rows, rows)

    def test_parse_adoption_sort_from_query_params(self) -> None:
        _, dashboard = _reload()
        cases = [
            ({}, (None, False)),
            ({ADOPT_SORT_PARAM: SORT_KEY_SESSIONS}, (SORT_KEY_SESSIONS, False)),
            (
                {ADOPT_SORT_PARAM: SORT_KEY_SESSIONS, ADOPT_DIR_PARAM: SORT_DESC},
                (SORT_KEY_SESSIONS, True),
            ),
            (
                {ADOPT_SORT_PARAM: SORT_KEY_SESSIONS, ADOPT_DIR_PARAM: SORT_ASC},
                (SORT_KEY_SESSIONS, False),
            ),
            ({ADOPT_SORT_PARAM: "rate", ADOPT_DIR_PARAM: SORT_DESC}, ("rate", True)),
            # An unknown / stale column degrades to the default order rather
            # than raising.
            ({ADOPT_SORT_PARAM: "bogus", ADOPT_DIR_PARAM: SORT_DESC}, (None, False)),
            ({ADOPT_DIR_PARAM: SORT_DESC}, (None, False)),
        ]
        for query_params, expected in cases:
            with self.subTest(params=query_params):
                self.assertEqual(
                    dashboard.parse_skill_adoption_sort(query_params),
                    expected,
                )
        # `params` is the public keyword; callers may pass it by name.
        self.assertEqual(
            dashboard.parse_skill_adoption_sort(
                params={ADOPT_SORT_PARAM: SORT_KEY_SESSIONS},
            ),
            (SORT_KEY_SESSIONS, False),
        )
