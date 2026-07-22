# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory filter-option and run-filtering tests."""

import unittest


from orchestrator import trajectory_reader as tr


_KIND = "kind"


_NAME = "name"


_CONTENT_KEY = "content"


_TOOL_CALL = "tool_call"


_ASSISTANT_MESSAGE = "assistant_message"


_BACKEND_CLAUDE = "claude"


_BACKEND_CODEX = "codex"


_REPO_A = "a/a"


_REPO_B = "b/b"


_STAGE_IMPLEMENTING = "implementing"


_STAGE_IN_REVIEW = "in_review"


_ROLE_DEVELOPER = "developer"


_TOOL_BASH = "Bash"


_TOOL_SKILL = "Skill"


_SKILL_DEVELOP = "develop"


_IGNORED = "ignored"


_TS = "2026-06-20T10:00:00+00:00"


_ISSUE = 42


def _record(**overrides):
    record = {
        "ts": _TS,
        "repo": "acme/widgets",
        "issue": _ISSUE,
        "event": "agent_trajectory",
        "stage": _STAGE_IMPLEMENTING,
        "agent_role": _ROLE_DEVELOPER,
        "backend": _BACKEND_CLAUDE,
        "steps": [],
    }
    record.update(overrides)
    return record


class FilterOptionsTest(unittest.TestCase):

    def test_distinct_sorted_non_empty(self) -> None:
        runs = [
            tr.parse_record(
                _record(repo=_REPO_B, backend=_BACKEND_CODEX, agent_role="reviewer",
                        stage=_STAGE_IN_REVIEW),
                seq=0,
            ),
            tr.parse_record(
                _record(repo=_REPO_A, backend=_BACKEND_CLAUDE, agent_role=_ROLE_DEVELOPER,
                        stage=_STAGE_IMPLEMENTING),
                seq=1,
            ),
            tr.parse_record(
                _record(repo=_REPO_A, backend=_BACKEND_CLAUDE, agent_role="",
                        stage=""),
                seq=2,
            ),
        ]
        opts = tr.filter_options(runs)
        self.assertEqual(opts.repos, (_REPO_A, _REPO_B))
        self.assertEqual(opts.backends, (_BACKEND_CLAUDE, _BACKEND_CODEX))
        # Empty role / stage are dropped, not offered as a blank choice.
        self.assertEqual(opts.agent_roles, (_ROLE_DEVELOPER, "reviewer"))
        self.assertEqual(opts.stages, (_STAGE_IMPLEMENTING, _STAGE_IN_REVIEW))


class _FilterRunsSupport(unittest.TestCase):
    def _runs(self):
        return [
            tr.parse_record(
                _record(issue=1, repo=_REPO_A, backend=_BACKEND_CLAUDE,
                        agent_role=_ROLE_DEVELOPER, stage=_STAGE_IMPLEMENTING,
                        output="resolved the bug",
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                                _CONTENT_KEY: "grep needle file.py"}],
                        skills_triggered=[_SKILL_DEVELOP]),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=2, repo=_REPO_B, backend=_BACKEND_CODEX,
                        agent_role="reviewer", stage=_STAGE_IN_REVIEW,
                        output="looks good"),
                seq=1,
            ),
        ]


class FilterRunSelectionTest(_FilterRunsSupport):
    def test_no_filters_returns_all(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_repo_and_issue_exact_match(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, repo=_REPO_A)], [1]
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, issue=2)], [2]
        )

    def test_multi_value_filters(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, backends=[_BACKEND_CODEX])], [2]
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, agent_roles=[_ROLE_DEVELOPER])],
            [1],
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, stages=[_STAGE_IN_REVIEW])], [2]
        )

    def test_empty_multi_value_is_no_constraint(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs, backends=[])), 2)
        self.assertEqual(len(tr.filter_runs(runs, stages=None)), 2)

    def test_query_spans_output_steps_and_skills(self) -> None:
        runs = self._runs()
        # Output text.
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="resolved")], [1]
        )
        # Step content (a path inside a tool command).
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="file.py")], [1]
        )
        # Skill name, case-insensitive.
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="DEVELOP")], [1]
        )
        # Whitespace-only query is treated as no filter.
        self.assertEqual(len(tr.filter_runs(runs, query="   ")), 2)

    def test_query_matches_message_turn_content(self) -> None:
        # The newer `assistant_message` / `user_message` turns are steps
        # too, so the free-text search reaches their content like any
        # tool payload.
        runs = [
            tr.parse_record(
                _record(issue=1, steps=[
                    {_KIND: _ASSISTANT_MESSAGE,
                     _CONTENT_KEY: "I will refactor the cache layer"}]),
                seq=0,
            ),
            tr.parse_record(_record(issue=2), seq=1),
        ]
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="refactor")], [1]
        )

    def test_filters_combine_preserving_order(self) -> None:
        base_runs = self._runs()
        matching_later = tr.parse_record(
            _record(
                issue=1,
                repo=_REPO_A,
                backend=_BACKEND_CLAUDE,
                agent_role=_ROLE_DEVELOPER,
                stage=_STAGE_IMPLEMENTING,
                output="resolved another bug",
            ),
            seq=9,
        )
        matching_fixture = tr.parse_record(
            _record(
                issue=1,
                repo=_REPO_A,
                backend=_BACKEND_CLAUDE,
                agent_role=_ROLE_DEVELOPER,
                stage=_STAGE_IMPLEMENTING,
                user_input=_IGNORED,
                output="resolved fixture bug",
            ),
            seq=8,
        )
        runs = [
            base_runs[0],
            matching_fixture,
            matching_later,
            base_runs[1],
        ]
        self.assertEqual(
            [
                run.seq
                for run in tr.filter_runs(
                    runs,
                    repo=_REPO_A,
                    backends=[_BACKEND_CLAUDE],
                    agent_roles=[_ROLE_DEVELOPER],
                    stages=[_STAGE_IMPLEMENTING],
                    issue=1,
                    query="RESOLVED",
                    exclude_fixtures=True,
                )
            ],
            [0, 9],
        )


class FilterRunFixtureTest(_FilterRunsSupport):
    def test_exclude_fixtures_default_off(self) -> None:
        # Backward-compatible default: fixtures are kept unless asked to
        # drop them.
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=1),
        ]
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_exclude_fixtures_drops_every_tell(self) -> None:
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=1),
            tr.parse_record(_record(issue=3, session_id="sess-7"), seq=2),
            tr.parse_record(_record(issue=4, steps=[
                {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                 _CONTENT_KEY: _SKILL_DEVELOP}]), seq=3),
        ]
        kept = tr.filter_runs(runs, exclude_fixtures=True)
        self.assertEqual([run.issue for run in kept], [1])

    def test_fixture_exclusion_combines_with_filters(self) -> None:
        # An issue filter that selects a fixture still drops it.
        runs = [
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=0),
        ]
        self.assertEqual(
            tr.filter_runs(runs, issue=2, exclude_fixtures=True), []
        )
