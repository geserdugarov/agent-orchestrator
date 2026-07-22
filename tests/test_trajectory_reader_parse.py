# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory record parsing tests."""

import unittest


from orchestrator import trajectory_reader as tr


_KIND = "kind"


_NAME = "name"


_CONTENT_KEY = "content"


_TOOL_ID = "tool_id"


_TOOL_CALL = "tool_call"


_TOOL_RESULT = "tool_result"


_BACKEND_CLAUDE = "claude"


_STAGE_IMPLEMENTING = "implementing"


_ROLE_DEVELOPER = "developer"


_TOOL_BASH = "Bash"


_TOOL_EDIT = "Edit"


_SKILL_DEVELOP = "develop"


_T1 = "t1"


_PROMPT_DO_THING = "do the thing"


_DONE = "done"


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


class ParseRecordShapeTest(unittest.TestCase):
    def test_full_record_round_trips(self) -> None:
        record = _record(
            session_id="sess-1",
            review_round=2,
            retry_count=1,
            user_input=_PROMPT_DO_THING,
            system_prompt="you are an agent",
            output=_DONE,
            tools=[_TOOL_BASH, _TOOL_EDIT],
            skills_triggered=[_SKILL_DEVELOP],
            skills_available=[_SKILL_DEVELOP, "review"],
            steps=[
                {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                 _TOOL_ID: _T1, _CONTENT_KEY: "ls -la"},
                {_KIND: _TOOL_RESULT, _NAME: None,
                 _TOOL_ID: _T1, _CONTENT_KEY: "listing"},
            ],
            truncated=True,
        )
        run = tr.parse_record(record, seq=3)
        assert run is not None
        for field, expected in (
            ("seq", 3),
            ("issue", _ISSUE),
            ("review_round", 2),
            ("retry_count", 1),
            ("tools", (_TOOL_BASH, _TOOL_EDIT)),
            ("skills_triggered", (_SKILL_DEVELOP,)),
        ):
            self.assertEqual(getattr(run, field), expected, field)
        self.assertTrue(run.truncated)
        self.assertEqual(run.step_count, 2)
        self.assertEqual(run.tool_calls, 1)
        # A result step's missing name normalises to "" so the page
        # never has to guard against None.
        self.assertEqual(run.steps[1].name, "")
        self.assertTrue(run.steps[0].is_call)
        self.assertTrue(run.steps[1].is_result)

    def test_non_dict_returns_none(self) -> None:
        self.assertIsNone(tr.parse_record("nope", seq=0))
        self.assertIsNone(tr.parse_record(["a", "b"], seq=0))

    def test_accepts_obj_keyword(self) -> None:
        # `obj` is the public keyword; callers may pass the record by name.
        run = tr.parse_record(obj=_record(issue=7), seq=0)
        assert run is not None
        self.assertEqual(run.issue, 7)

    def test_foreign_event_returns_none(self) -> None:
        self.assertIsNone(
            tr.parse_record(_record(event="agent_exit"), seq=0)
        )
        self.assertIsNone(
            tr.parse_record({"repo": "x", "issue": 1}, seq=0)
        )

    def test_missing_optionals_default_cleanly(self) -> None:
        run = tr.parse_record(_record(), seq=0)
        assert run is not None
        self.assertEqual(run.session_id, "")
        self.assertIsNone(run.review_round)
        self.assertIsNone(run.retry_count)
        self.assertEqual(run.tools, ())
        self.assertEqual(run.steps, ())
        self.assertIsNone(run.run_usage)
        self.assertEqual(run.turns, ())
        self.assertFalse(run.truncated)


class ParseRecordCoercionTest(unittest.TestCase):
    def test_step_without_kind_is_dropped(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {_NAME: _TOOL_BASH, _CONTENT_KEY: "x"},     # no kind -> dropped
                {_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT},
                "not-a-dict",                          # dropped
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.step_count, 1)
        self.assertEqual(run.steps[0].name, _TOOL_EDIT)

    def test_none_step_content_becomes_empty(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {_KIND: _TOOL_RESULT, _TOOL_ID: _T1, _CONTENT_KEY: None},
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.steps[0].content, "")

    def test_issue_coerced_bad_value_defaults_zero(self) -> None:
        coerced = tr.parse_record(_record(issue="7"), seq=0)
        self.assertEqual(coerced.issue, 7)
        uncoercible = tr.parse_record(_record(issue="bad"), seq=0)
        self.assertEqual(uncoercible.issue, 0)

    def test_review_round_string_coerced(self) -> None:
        run = tr.parse_record(_record(review_round="3"), seq=0)
        self.assertEqual(run.review_round, 3)
