# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-matrix sorting tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ROLE_DEVELOPER = "developer"


ROLE_REVIEWER = "reviewer"


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


REPO_C_CELL_FRAGMENT = ">c/repo<"


MTX_SORT_PARAM = "mtx_sort"


MTX_DIR_PARAM = "mtx_dir"


SORT_ASC = "asc"


SORT_DESC = "desc"


MTX_SORT_KEYS = (
    "repo",
    "role",
    "backend",
    "skill",
    "runs",
    "skill_runs",
    "rate",
)


SORT_KEY_RUNS = "runs"


class _SkillMatrixSortSupport(unittest.TestCase):
    """The per-skill trigger matrix column headers are clickable sort
    controls: each is an anchor writing `mtx_sort` / `mtx_dir` query
    params, and the caller feeds the parsed `(column, direction)` back
    into `_skill_matrix_html` so the rows re-sort on that column and the
    active header shows a ▲ / ▼ indicator.
    """

    def _row(self, *row_fields):
        from orchestrator.analytics.read import SkillTriggerMatrixRow

        return SkillTriggerMatrixRow(
            repo=row_fields[0],
            skill=row_fields[1],
            agent_role=row_fields[2],
            backend=row_fields[3],
            runs=row_fields[4],
            skill_runs=row_fields[4] if len(row_fields) == 5 else row_fields[5],
        )

    def _rows(self):
        # Distinct repo / runs values per row so an ordering assertion can
        # key off either without ambiguity.
        return [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 2, 1),
            self._row("a/repo", "beta", ROLE_REVIEWER, BACKEND_CODEX, 9, 9),
            self._row("c/repo", "gamma", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, 0),
        ]


class SkillMatrixHeaderSortTest(_SkillMatrixSortSupport):
    def test_headers_link_to_self_for_sorting(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(self._rows())
        # Every column is an in-tab anchor pointing at its own sort param.
        for key in MTX_SORT_KEYS:
            self.assertIn(f"?{MTX_SORT_PARAM}={key}&{MTX_DIR_PARAM}=", html)
        self.assertIn('target="_self"', html)
        # Text columns default a first click to ascending, numeric ones to
        # descending (largest first is the interesting end for counts).
        self.assertIn(f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_ASC}", html)
        self.assertIn(f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_DESC}", html)
        # With no active sort no header carries a direction indicator (the
        # class still appears in the CSS block, so match the span markup).
        self.assertNotIn('<span class="orch-skillmatrix-sort">', html)

    def test_descending_shows_down_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(),
            sort_key=SORT_KEY_RUNS,
            descending=True,
        )
        # Exactly one column is marked active, and it shows the ▼ arrow.
        self.assertEqual(
            html.count('<span class="orch-skillmatrix-sort">'),
            1,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▼</span>',
            html,
        )
        # Re-clicking the active (descending) column flips it to ascending.
        self.assertIn(f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_ASC}", html)

    def test_ascending_shows_up_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(),
            sort_key="repo",
            descending=False,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▲</span>',
            html,
        )
        self.assertIn(f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_DESC}", html)


class SkillMatrixRowSortTest(_SkillMatrixSortSupport):
    def test_rows_render_in_selected_column_order(self) -> None:
        _, dashboard = _reload()
        asc = dashboard._skill_matrix_html(
            self._rows(),
            sort_key=SORT_KEY_RUNS,
            descending=False,
        )
        # runs 2 < 5 < 9 -> repos b, c, a in that order.
        self.assertLess(asc.index(">b/repo<"), asc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(asc.index(REPO_C_CELL_FRAGMENT), asc.index(">a/repo<"))
        desc = dashboard._skill_matrix_html(
            self._rows(),
            sort_key=SORT_KEY_RUNS,
            descending=True,
        )
        self.assertLess(desc.index(">a/repo<"), desc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(desc.index(REPO_C_CELL_FRAGMENT), desc.index(">b/repo<"))

    def test_unsorted_defaults_repo_asc_rate_desc(self) -> None:
        # No sort key -> the default view orders rows by repo ascending,
        # then trigger rate descending within each repo, so each repo's
        # hottest skills lead. Two rows share a repo with different rates
        # so both keys are exercised (skills identify the rows uniquely).
        _, dashboard = _reload()
        rows = [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "beta", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "gamma", ROLE_REVIEWER, BACKEND_CODEX, 4, 3),
        ]
        html = dashboard._skill_matrix_html(rows)
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
        from orchestrator import dashboard_skill_matrix

        rows = self._rows()
        sorted_rows = dashboard_skill_matrix._sort_skill_matrix_rows(rows, None, False)
        self.assertEqual(sorted_rows, rows)
        sorted_rows = dashboard_skill_matrix._sort_skill_matrix_rows(rows, "bogus", True)
        self.assertEqual(sorted_rows, rows)

    def test_parse_matrix_sort_from_query_params(self) -> None:
        _, dashboard = _reload()
        cases = [
            ({}, (None, False)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS}, (SORT_KEY_RUNS, False)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS, MTX_DIR_PARAM: SORT_DESC}, (SORT_KEY_RUNS, True)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS, MTX_DIR_PARAM: SORT_ASC}, (SORT_KEY_RUNS, False)),
            ({MTX_SORT_PARAM: "rate", MTX_DIR_PARAM: SORT_DESC}, ("rate", True)),
            # An unknown / stale column degrades to the default order
            # rather than raising.
            ({MTX_SORT_PARAM: "bogus", MTX_DIR_PARAM: SORT_DESC}, (None, False)),
            ({MTX_DIR_PARAM: SORT_DESC}, (None, False)),
        ]
        for query_params, expected in cases:
            with self.subTest(params=query_params):
                self.assertEqual(
                    dashboard.parse_skill_matrix_sort(query_params),
                    expected,
                )
        # `params` is the public keyword; callers may pass it by name.
        self.assertEqual(
            dashboard.parse_skill_matrix_sort(params={MTX_SORT_PARAM: SORT_KEY_RUNS}),
            (SORT_KEY_RUNS, False),
        )
