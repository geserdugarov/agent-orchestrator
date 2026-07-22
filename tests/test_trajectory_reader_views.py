# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory summary, label, timeline, and fixture-identification tests."""

import unittest


from orchestrator import trajectory_reader as tr


_KIND = "kind"


_NAME = "name"


_CONTENT_KEY = "content"


_TOOL_ID = "tool_id"


_COST_USD = "cost_usd"


_COST_SOURCE = "cost_source"


_TOOL_CALL = "tool_call"


_TOOL_RESULT = "tool_result"


_ASSISTANT_MESSAGE = "assistant_message"


_TL_PROMPT = "prompt"


_TL_OUTPUT = "output"


_BACKEND_CLAUDE = "claude"


_REPORTED = "reported"


_UNKNOWN_PRICE = "unknown-price"


_REPO_A = "a/a"


_REPO_B = "b/b"


_STAGE_IMPLEMENTING = "implementing"


_ROLE_DEVELOPER = "developer"


_TOOL_BASH = "Bash"


_TOOL_EDIT = "Edit"


_TOOL_SKILL = "Skill"


_SKILL_DEVELOP = "develop"


_T1 = "t1"


_PROMPT_DO_THING = "do the thing"


_IGNORED = "ignored"


_DONE = "done"


_LS = "ls"


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


class SummarizeTest(unittest.TestCase):

    def test_counts(self) -> None:
        runs = [
            tr.parse_record(
                _record(issue=1, repo=_REPO_A,
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH},
                               {_KIND: _TOOL_RESULT, _TOOL_ID: "t"}],
                        truncated=True),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=1, repo=_REPO_A,
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT}]),
                seq=1,
            ),
            tr.parse_record(_record(issue=2, repo=_REPO_B), seq=2),
        ]
        summary = tr.summarize(runs)
        self.assertEqual(summary.total_runs, 3)
        # Two runs share (a/a, 1); (b/b, 2) is the third distinct issue.
        self.assertEqual(summary.distinct_issues, 2)
        self.assertEqual(summary.distinct_repos, 2)
        self.assertEqual(summary.total_tool_calls, 2)
        self.assertEqual(summary.truncated_runs, 1)

    def test_empty(self) -> None:
        summary = tr.summarize([])
        self.assertEqual(
            (summary.total_runs, summary.distinct_issues, summary.distinct_repos,
             summary.total_tool_calls, summary.truncated_runs, summary.total_cost_usd),
            (0, 0, 0, 0, 0, float()),
        )

    def test_total_cost_sums_only_priced_runs(self) -> None:
        # The KPI sums the authoritative run cost; a run with no run_usage
        # (pre-usage record) or an unpriced cost (None) contributes nothing
        # rather than a spurious 0.
        runs = [
            tr.parse_record(_record(
                issue=1,
                run_usage={_COST_USD: 0.8, _COST_SOURCE: _REPORTED}),
                seq=0),
            tr.parse_record(_record(
                issue=2,
                run_usage={_COST_USD: 0.2, _COST_SOURCE: "estimated"}),
                seq=1),
            tr.parse_record(_record(issue=3), seq=2),
            tr.parse_record(_record(
                issue=4,
                run_usage={_COST_USD: None, _COST_SOURCE: _UNKNOWN_PRICE}),
                seq=3),
        ]
        self.assertAlmostEqual(tr.summarize(runs).total_cost_usd, 1.0)


class LabelTest(unittest.TestCase):

    def test_label_carries_issue_repo_and_round(self) -> None:
        run = tr.parse_record(
            _record(issue=_ISSUE, repo=_REPO_A, stage=_STAGE_IMPLEMENTING,
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=1),
            seq=0,
        )
        label = run.label()
        self.assertIn("#42", label)
        self.assertIn(_REPO_A, label)
        self.assertIn("implementing/developer", label)
        self.assertIn("round 1", label)

    def test_label_without_round_omits_it(self) -> None:
        run = tr.parse_record(_record(), seq=0)
        self.assertNotIn("round", run.label())

    def test_detail_label_drops_issue_and_repo(self) -> None:
        run = tr.parse_record(
            _record(issue=_ISSUE, repo=_REPO_A, stage="documenting",
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=0),
            seq=0,
        )
        detail = run.detail_label()
        self.assertIn("documenting/developer · claude · round 0", detail)
        self.assertIn(run.ts, detail)
        # The repo / issue are picked separately, so they are dropped here.
        self.assertNotIn("#42", detail)
        self.assertNotIn(_REPO_A, detail)

    def test_label_is_issue_repo_plus_detail_label(self) -> None:
        run = tr.parse_record(
            _record(issue=7, repo=_REPO_A, stage=_STAGE_IMPLEMENTING,
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=1),
            seq=0,
        )
        self.assertEqual(
            run.label(), "#7 a/a · {0}".format(run.detail_label())
        )


class TimelineTest(unittest.TestCase):
    """`TrajectoryRun.timeline` normalizes old and new records alike."""

    def test_old_step_record_wraps_prompt_and_output(self) -> None:
        # A legacy record predates the text-turn timeline: its steps are
        # only tool_call / tool_result. The normalized timeline still
        # brackets them with the prompt and the final output, in order.
        run = tr.parse_record(
            _record(
                user_input=_PROMPT_DO_THING,
                output="all done",
                steps=[
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                     _TOOL_ID: _T1, _CONTENT_KEY: _LS},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: _T1,
                     _CONTENT_KEY: "calc.py"},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [entry.kind for entry in run.timeline],
            [_TL_PROMPT, _TOOL_CALL, _TOOL_RESULT, _TL_OUTPUT],
        )
        self.assertTrue(run.timeline[0].is_prompt)
        self.assertEqual(run.timeline[0].content, _PROMPT_DO_THING)
        self.assertTrue(run.timeline[-1].is_output)
        self.assertEqual(run.timeline[-1].content, "all done")
        # The middle tool_call keeps its name / id; the brackets carry none.
        call = run.timeline[1]
        self.assertEqual(call.name, _TOOL_BASH)
        self.assertEqual(call.tool_id, _T1)
        self.assertEqual(run.timeline[0].name, "")
        self.assertEqual(run.timeline[0].tool_id, "")

    def test_mixed_timeline_keeps_interleaved_turns(self) -> None:
        # A record written since the timeline feature interleaves
        # assistant / user text turns with the tool steps; the normalized
        # timeline keeps stream order and adds the prompt / output brackets.
        run = tr.parse_record(
            _record(
                user_input="fix the parser",
                output="fixed",
                steps=[
                    {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: "let me look"},
                    {_KIND: _TOOL_CALL, _NAME: "Read", _TOOL_ID: "r1",
                     _CONTENT_KEY: "open x.py"},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: "r1",
                     _CONTENT_KEY: "body"},
                    {_KIND: "user_message", _CONTENT_KEY: "now ship it"},
                    {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: _DONE},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [entry.kind for entry in run.timeline],
            [_TL_PROMPT, _ASSISTANT_MESSAGE, _TOOL_CALL, _TOOL_RESULT,
             "user_message", _ASSISTANT_MESSAGE, _TL_OUTPUT],
        )

    def test_tool_calls_count_excludes_message_turns(self) -> None:
        # The message turns are steps but must not be counted as tool
        # calls -- the tally stays correct across record vintages.
        run = tr.parse_record(
            _record(steps=[
                {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: "thinking"},
                {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH, _CONTENT_KEY: _LS},
                {_KIND: _TOOL_RESULT, _TOOL_ID: "t", _CONTENT_KEY: "out"},
                {_KIND: "user_message", _CONTENT_KEY: "go on"},
                {_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT, _CONTENT_KEY: "patch"},
            ]),
            seq=0,
        )
        self.assertEqual(run.step_count, 5)
        self.assertEqual(run.tool_calls, 2)

    def test_brackets_omitted_when_field_empty(self) -> None:
        # No prompt and no output: the timeline is exactly the steps.
        run = tr.parse_record(
            _record(user_input="", output="",
                    steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH}]),
            seq=0,
        )
        self.assertEqual([entry.kind for entry in run.timeline], [_TOOL_CALL])

    def test_prompt_only_record_is_single_bracket(self) -> None:
        run = tr.parse_record(
            _record(user_input="just a prompt", output=""), seq=0
        )
        timeline = run.timeline
        self.assertEqual([entry.kind for entry in timeline], [_TL_PROMPT])
        self.assertEqual(timeline[0].content, "just a prompt")

    def test_empty_record_has_empty_timeline(self) -> None:
        run = tr.parse_record(_record(user_input="", output=""), seq=0)
        self.assertEqual(run.timeline, ())


class FixtureIdentificationTest(unittest.TestCase):
    """`TrajectoryRun.is_fixture` flags synthetic test-suite records."""

    def test_ignored_prompt_is_fixture(self) -> None:
        self.assertTrue(
            tr.parse_record(_record(user_input=_IGNORED), seq=0).is_fixture
        )
        # Case and surrounding whitespace do not hide the sentinel.
        self.assertTrue(
            tr.parse_record(
                _record(user_input="  IGNORED "), seq=0
            ).is_fixture
        )

    def test_sess_session_id_is_fixture(self) -> None:
        self.assertTrue(
            tr.parse_record(_record(session_id="sess-dev"), seq=0).is_fixture
        )
        self.assertTrue(
            tr.parse_record(_record(session_id="sess-1"), seq=0).is_fixture
        )

    def test_skill_only_run_is_fixture(self) -> None:
        run = tr.parse_record(
            _record(
                user_input="real prompt",
                session_id="uuid-9",
                steps=[
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: _SKILL_DEVELOP},
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: "review"},
                ],
            ),
            seq=0,
        )
        self.assertTrue(run.is_fixture)

    def test_real_run_is_not_fixture(self) -> None:
        # A real prompt, a uuid session id, and mixed real tool work
        # (a Skill call among Bash / its result): no tell fires.
        run = tr.parse_record(
            _record(
                user_input="please fix issue 7",
                session_id="0f9a3c2e-1b4d-4a77-9c12-abcdef012345",
                steps=[
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: _SKILL_DEVELOP},
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                     _CONTENT_KEY: "pytest"},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: "t", _CONTENT_KEY: "ok"},
                ],
            ),
            seq=0,
        )
        self.assertFalse(run.is_fixture)

    def test_no_steps_run_is_not_skill_only(self) -> None:
        # An empty step list must not be read as a Skill-only run; only
        # the prompt / session tells can flag a stepless record.
        run = tr.parse_record(
            _record(user_input="real", session_id="abc123"), seq=0
        )
        self.assertFalse(run.is_fixture)
