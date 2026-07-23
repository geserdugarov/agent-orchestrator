# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_claude_events as _claude
from tests import usage_trajectory_projections as _projections


class ClaudeTrajectoryTest(unittest.TestCase):
    """``_usage.parse_claude_trajectory`` over synthetic ``stream-json`` runs.

    The init frame's ``tools`` array is the offered-tools set; in stream
    order, ``text`` blocks in ``assistant`` messages are ``assistant_message``
    turns and their ``tool_use`` blocks are calls, while ``text`` blocks in
    ``user`` messages are ``user_message`` turns and their ``tool_result``
    blocks are results (joined by ``tool_use_id``); the ``result`` frame's
    ``result`` string is the final output. Raw inputs / outputs / text ride
    along verbatim -- this layer classifies, it does not redact.
    """

    def test_extracts_tools_skills_and_final_output(self) -> None:
        stdout = _jsonl.jsonl(
            _claude.system_init(
                tools=[_usage_cases.BASH_TOOL, _usage_cases.READ_TOOL, _usage_cases.SKILL_TOOL],
                skills=[_usage_cases.DEVELOP, _usage_cases.REVIEW],
            ),
            _claude.assistant(
                content_blocks=[
                    _jsonl.text("let me look"),
                    _jsonl.tool_use(
                        _usage_cases.BASH_TOOL,
                        {_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                        id=_usage_cases.TOOL_USE_A_ID,
                    ),
                ],
            ),
            _jsonl.user(
                [
                    _jsonl.tool_result(_usage_cases.TOOL_USE_A_ID, "calc.py\n"),
                ]
            ),
            _claude.assistant(
                id="msg_2",
                content_blocks=[
                    _claude.skill_use(
                        _usage_cases.DEVELOP,
                        id=_usage_cases.TOOL_USE_B_ID,
                    ),
                ],
            ),
            _claude.terminal_result(result="All done.", num_turns=2),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(
            _projections.claude_summary(trajectory),
            (
                _usage_cases.CLAUDE,
                None,
                (_usage_cases.BASH_TOOL, _usage_cases.READ_TOOL, _usage_cases.SKILL_TOOL),
                "All done.",
                (
                    (_usage_cases.DEVELOP, _usage_cases.REVIEW),
                    _usage_cases.DEVELOP_ONLY,
                ),
            ),
        )
        self.assertEqual(
            trajectory.steps,
            (
                _usage.TrajectoryStep(
                    kind=_usage_cases.ASSISTANT_MESSAGE_STEP,
                    turn=0,
                    content="let me look",
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_CALL_STEP,
                    name=_usage_cases.BASH_TOOL,
                    tool_id=_usage_cases.TOOL_USE_A_ID,
                    turn=0,
                    content={_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_RESULT_STEP,
                    tool_id=_usage_cases.TOOL_USE_A_ID,
                    content="calc.py\n",
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_CALL_STEP,
                    name=_usage_cases.SKILL_TOOL,
                    tool_id=_usage_cases.TOOL_USE_B_ID,
                    turn=1,
                    content={"skill": _usage_cases.DEVELOP},
                ),
            ),
        )

    def test_captures_text_turns_in_stream_order(
        self,
    ) -> None:
        # Full timeline: an assistant text turn, then a tool call + its
        # result and a user text turn in the same user message, then a closing
        # assistant text turn -- text turns are preserved inline with the tool
        # steps, in stream order, alongside the unchanged final output.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id="m1",
                content_blocks=[
                    _jsonl.text("let me check"),
                    _jsonl.tool_use(
                        _usage_cases.READ_TOOL, {_usage_cases.FILE_PATH_FIELD: _usage_cases.READ_FIXTURE_PATH}, id="tu1"
                    ),
                ],
            ),
            _jsonl.user(
                [
                    _jsonl.tool_result("tu1", "file body"),
                    _jsonl.text("now fix it"),
                ]
            ),
            _claude.assistant(id="m2", content_blocks=[_jsonl.text(_usage_cases.FINAL_OUTPUT)]),
            _claude.terminal_result(result="all set"),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.content) for step in trajectory.steps],
            [
                (_usage_cases.ASSISTANT_MESSAGE_STEP, "let me check"),
                (_usage_cases.TOOL_CALL_STEP, {_usage_cases.FILE_PATH_FIELD: _usage_cases.READ_FIXTURE_PATH}),
                (_usage_cases.TOOL_RESULT_STEP, "file body"),
                ("user_message", "now fix it"),
                (_usage_cases.ASSISTANT_MESSAGE_STEP, _usage_cases.FINAL_OUTPUT),
            ],
        )
        # Text turns carry no tool name / id.
        first = trajectory.steps[0]
        self.assertEqual(first.name, "")
        self.assertEqual(first.tool_id, "")
        self.assertEqual(trajectory.final_output, "all set")

    def test_skips_empty_or_nonstring_text_blocks(self) -> None:
        # An empty / missing / non-string text block does not create a
        # message step -- only non-empty string text turns are captured.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.MESSAGE_FIXTURE_ID,
                content_blocks=[
                    _jsonl.text(""),
                    {_usage_cases.TYPE_FIELD: _usage_cases.TEXT_FIELD},
                    _jsonl.text(7),
                ],
            ),
            _jsonl.user([_jsonl.text("")]),
        )
        self.assertEqual(_usage.parse_claude_trajectory(stdout).steps, ())

    def test_partial_frames_dedup_calls_and_results(self) -> None:
        # Defensive: a tool_use / tool_result block repeated across frames
        # (sharing its id) is one step, not two -- the same per-id de-dup
        # ``_usage.parse_claude_skills`` applies. Distinct ids stay distinct.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                content_blocks=[
                    _jsonl.tool_use(
                        _usage_cases.BASH_TOOL,
                        {_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                        id=_usage_cases.TOOL_USE_A_ID,
                    )
                ]
            ),
            _claude.assistant(
                content_blocks=[
                    _jsonl.tool_use(
                        _usage_cases.BASH_TOOL,
                        {_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                        id=_usage_cases.TOOL_USE_A_ID,
                    )
                ]
            ),
            _jsonl.user([_jsonl.tool_result(_usage_cases.TOOL_USE_A_ID, "out")]),
            _jsonl.user([_jsonl.tool_result(_usage_cases.TOOL_USE_A_ID, "out")]),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(
            [step.kind for step in trajectory.steps], [_usage_cases.TOOL_CALL_STEP, _usage_cases.TOOL_RESULT_STEP]
        )

    def test_missing_fields_yield_empty_sections(self) -> None:
        # No init frame, no capturable blocks, no result frame: every section
        # is empty / None, never an exception.
        stdout = _jsonl.jsonl(
            _claude.assistant(id=_usage_cases.MESSAGE_FIXTURE_ID, content_blocks=[]),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.steps, ())
        self.assertIsNone(trajectory.final_output)
        self.assertIsNone(trajectory.system_prompt)
        self.assertEqual(trajectory.skills, _usage.SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(
            _claude.assistant(
                id=_usage_cases.MESSAGE_FIXTURE_ID,
                content_blocks=[
                    _jsonl.tool_use(
                        _usage_cases.READ_TOOL,
                        {_usage_cases.FILE_PATH_FIELD: _usage_cases.READ_FIXTURE_PATH},
                        id=_usage_cases.TOOL_USE_A_ID,
                    )
                ],
            )
        )
        stdout = "\n".join(
            [
                "starting claude...",
                '{"type":"assistant","message"',
                good,
                "not json either",
            ]
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(len(trajectory.steps), 1)
        self.assertEqual(trajectory.steps[0].name, _usage_cases.READ_TOOL)

    def test_empty_stdout(self) -> None:
        self.assertEqual(_usage.parse_claude_trajectory(""), _usage.AgentTrajectory(backend=_usage_cases.CLAUDE))
