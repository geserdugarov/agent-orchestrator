# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory dashboard topbar, KPI, metadata, chip, and run-table HTML tests."""

from unittest import TestCase, mock


from orchestrator import trajectory_reader as tr

_TRUNCATED_FOOT_COST_TOTAL_TO = 11
_TRUNCATED_FOOT_COST_TOTAL_CO = 12.5


_KIND = "kind"


_TOOL_ID = "tool_id"


_TOOL_CALL = "tool_call"


_TOOL_RESULT = "tool_result"


_TOOL_BASH = "Bash"


_REPO_UNSAFE = "o/<r&>"


_ISSUE = 42


def _td():
    from orchestrator import trajectory_dashboard as td

    return td


def _run(**overrides):
    record = {
        "ts": "2026-06-20T10:00:00+00:00",
        "repo": "acme/widgets",
        "issue": _ISSUE,
        "event": "agent_trajectory",
        "stage": "implementing",
        "agent_role": "developer",
        "backend": "claude",
        "steps": [],
    }
    record.update(overrides)
    return tr.parse_record(record, seq=0)


class TopbarHtmlTest(TestCase):
    def test_carries_title_and_in_view_pill(self) -> None:
        html = _td()._topbar_html(10, 3)
        self.assertIn("orch-topbar", html)
        self.assertIn("Orchestrator Trajectories", html)
        self.assertIn("10 recorded", html)
        self.assertIn("3 / 10", html)


class KpiStripHtmlTest(TestCase):
    def test_tiles_truncated_foot_and_cost(self) -> None:
        summary = tr.TrajectorySummary(
            total_runs=5,
            distinct_issues=3,
            distinct_repos=2,
            total_tool_calls=_TRUNCATED_FOOT_COST_TOTAL_TO,
            truncated_runs=1,
            total_cost_usd=_TRUNCATED_FOOT_COST_TOTAL_CO,
        )
        html = _td()._kpi_strip_html(summary)
        self.assertIn("orch-kpis", html)
        for label in ("Runs", "Issues", "Repos", "Tool calls", "Total cost"):
            self.assertIn(">{0}</span>".format(label), html)
        self.assertIn("1 truncated", html)
        # Exact cents even above $10 -- the compact `fmt_money` would read
        # `$12`, dropping the authoritative figure's cents.
        self.assertIn(">$12.50</div>", html)

    def test_no_truncated_reads_none_and_zero_cost(self) -> None:
        html = _td()._kpi_strip_html(tr.TrajectorySummary(total_runs=2))
        self.assertIn("none truncated", html)
        self.assertIn(">$0.00</div>", html)


class MetaHtmlTest(TestCase):
    def test_only_present_fields_render(self) -> None:
        run = _run(session_id="sess-1", review_round=2)
        html = _td()._meta_html(run)
        self.assertIn(">Repo</div>", html)
        self.assertIn(">acme/widgets</div>", html)
        self.assertIn(">Review round</div>", html)
        self.assertIn(">sess-1</div>", html)
        # No retry_count on this run -> the tile is omitted entirely.
        self.assertNotIn(">Retry count</div>", html)

    def test_html_escaped(self) -> None:
        run = _run(repo=_REPO_UNSAFE)
        html = _td()._meta_html(run)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertNotIn(_REPO_UNSAFE, html)


class ChipsHtmlTest(TestCase):
    def test_label_and_pills(self) -> None:
        html = _td()._labeled_chips_html("Tools offered", [_TOOL_BASH, "Edit"])
        self.assertIn("Tools offered", html)
        self.assertIn(">Bash</span>", html)
        self.assertIn(">Edit</span>", html)

    def test_empty_is_blank(self) -> None:
        self.assertEqual(_td()._labeled_chips_html("Tools", []), "")

    def test_empty_marker_renders_none_state(self) -> None:
        # With a marker, an empty list still renders the row and flags the
        # chip with the `none` empty-state class instead of a real pill.
        html = _td()._labeled_chips_html("Skills triggered", [], empty_marker="none")
        self.assertIn(">Skills triggered</span>", html)
        self.assertIn('class="orch-traj-chip none"', html)
        self.assertIn(">none</span>", html)

    def test_escaped(self) -> None:
        html = _td()._labeled_chips_html("Skills", ["<x>"])
        self.assertIn("&lt;x&gt;", html)
        self.assertNotIn("<x>", html)

    def test_render_shows_empty_skills_triggered(self) -> None:
        # A session that fired no skill still shows the row, marked `none`, so
        # it is distinguishable from an omitted row; the equally-empty Tools
        # and Skills-available rows stay omitted.
        blob = self._render_chips()
        self.assertIn(">Skills triggered</span>", blob)
        self.assertIn('class="orch-traj-chip none"', blob)
        self.assertIn(">none</span>", blob)
        self.assertNotIn("Tools offered", blob)
        self.assertNotIn("Skills available", blob)

    def test_render_triggered_skills_are_plain_chips(self) -> None:
        blob = self._render_chips(skills_triggered=["develop", "review"])
        self.assertIn(">develop</span>", blob)
        self.assertIn(">review</span>", blob)
        self.assertNotIn('class="orch-traj-chip none"', blob)

    def _render_chips(self, **overrides) -> str:
        st = mock.Mock()
        _td()._render_run_usage_and_chips(st, _run(**overrides))
        return "".join(call.args[0] for call in st.markdown.call_args_list)


class RunsTableHtmlTest(TestCase):
    def test_headers_and_row_cells(self) -> None:
        run = _run(
            issue=_ISSUE,
            review_round=1,
            steps=[{_KIND: _TOOL_CALL, "name": _TOOL_BASH}, {_KIND: _TOOL_RESULT, _TOOL_ID: "t"}],
        )
        html = _td()._runs_table_html([run])
        for header in ("Issue", "Repo", "Stage", "Role", "Backend", "Round", "Steps", "Tool calls", "Recorded"):
            self.assertIn(">{0}</th>".format(header), html)
        self.assertIn("#42", html)
        self.assertIn(">acme/widgets</td>", html)
        # 2 steps, 1 of which is a tool call.
        self.assertIn(">2</td>", html)
        self.assertIn(">1</td>", html)

    def test_repo_escaped(self) -> None:
        html = _td()._runs_table_html([_run(repo=_REPO_UNSAFE)])
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertNotIn(_REPO_UNSAFE, html)

    def test_fixture_row_flagged(self) -> None:
        # `ignored` is the sentinel prompt that marks a synthetic fixture.
        run = _run(user_input="ignored")
        self.assertTrue(run.is_fixture)
        html = _td()._runs_table_html([run])
        self.assertIn('<tr class="fixture">', html)
        self.assertIn("orch-traj-fixture-tag", html)
        self.assertIn(">fixture</span>", html)

    def test_real_row_not_flagged(self) -> None:
        run = _run()
        self.assertFalse(run.is_fixture)
        html = _td()._runs_table_html([run])
        self.assertNotIn('class="fixture"', html)
        self.assertNotIn("orch-traj-fixture-tag", html)
