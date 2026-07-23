# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex trajectory step parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexTrajectoryStepsTest(unittest.TestCase):
    """``_usage.parse_codex_trajectory`` over synthetic ``codex exec --json`` runs.

    Codex's tool surface is the shell: each ``command_execution`` is one call
    (its ``command``) plus one result (its ``aggregated_output``), deduped by
    the shared ``item.id`` across the started/completed pair; each
    ``agent_message`` is one ``assistant_message`` text turn (its ``text``),
    captured in stream order. The last ``agent_message`` ``text`` is also the
    final output; ``tools`` / ``system_prompt`` stay empty (no confirmed codex
    frame exposes them).
    """

    def test_extracts_steps_skills_and_final_output(self) -> None:
        stdout = _jsonl.jsonl(
            {_usage_cases.TYPE_FIELD: _usage_cases.THREAD_STARTED_EVENT, "thread_id": _usage_cases.TASK_ONE_ID},
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                _usage_cases.DEVELOP_SKILL_READ_COMMAND,
                started=True,
                status=_usage_cases.IN_PROGRESS_STATUS,
            ),
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                _usage_cases.DEVELOP_SKILL_READ_COMMAND,
                status=_usage_cases.COMPLETED_STATUS,
                exit_code=0,
                aggregated_output="# Developer skill\n",
            ),
            _codex.command(
                _usage_cases.ITEM_TWO_ID,
                "/bin/bash -lc 'git diff -- calc.py'",
                status=_usage_cases.COMPLETED_STATUS,
                exit_code=0,
                aggregated_output="diff --git ...\n",
            ),
            _codex.agent_message(_usage_cases.ITEM_THREE_ID, _usage_cases.APPROVAL_MESSAGE),
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.backend, _usage_cases.CODEX)
        self.assertIsNone(trajectory.system_prompt)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.final_output, _usage_cases.APPROVAL_MESSAGE)
        # SKILL.md read surfaces in the names-only skills extractor.
        self.assertEqual(trajectory.skills.triggered, _usage_cases.DEVELOP_ONLY)
        # started + completed for item_1 collapse to one call + one result;
        # the trailing agent_message rides along as an assistant_message turn
        # (and is also the final output).
        self.assertEqual(
            trajectory.steps,
            (
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_CALL_STEP,
                    name="command_execution",
                    tool_id=_usage_cases.ITEM_ONE_ID,
                    content=_usage_cases.DEVELOP_SKILL_READ_COMMAND,
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_RESULT_STEP, tool_id=_usage_cases.ITEM_ONE_ID, content="# Developer skill\n"
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_CALL_STEP,
                    name="command_execution",
                    tool_id=_usage_cases.ITEM_TWO_ID,
                    content="/bin/bash -lc 'git diff -- calc.py'",
                ),
                _usage.TrajectoryStep(
                    kind=_usage_cases.TOOL_RESULT_STEP, tool_id=_usage_cases.ITEM_TWO_ID, content="diff --git ...\n"
                ),
                _usage.TrajectoryStep(kind=_usage_cases.ASSISTANT_MESSAGE_STEP, content=_usage_cases.APPROVAL_MESSAGE),
            ),
        )

    def test_agent_messages_become_ordered_turns(self) -> None:
        # Each agent_message item becomes an assistant_message turn, kept in
        # stream order relative to the command steps; the last one is still the
        # final output.
        stdout = _jsonl.jsonl(
            _codex.agent_message(_usage_cases.AGENT_MESSAGE_ID, "starting"),
            _codex.command(
                "c1",
                _usage_cases.SHELL_LIST_COMMAND,
                status=_usage_cases.COMPLETED_STATUS,
                exit_code=0,
                aggregated_output=_usage_cases.COMMAND_OUTPUT,
            ),
            _codex.agent_message("a2", "all done"),
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.content) for step in trajectory.steps],
            [
                (_usage_cases.ASSISTANT_MESSAGE_STEP, "starting"),
                (_usage_cases.TOOL_CALL_STEP, _usage_cases.SHELL_LIST_COMMAND),
                (_usage_cases.TOOL_RESULT_STEP, _usage_cases.COMMAND_OUTPUT),
                (_usage_cases.ASSISTANT_MESSAGE_STEP, "all done"),
            ],
        )
        self.assertEqual(trajectory.final_output, "all done")

    def test_started_completed_message_collapses(self) -> None:
        # A started + completed agent_message sharing an item.id is one turn
        # (last text wins), mirroring the command started/completed collapse.
        stdout = _jsonl.jsonl(
            _codex.agent_message(_usage_cases.AGENT_MESSAGE_ID, "partial", started=True),
            _codex.agent_message(_usage_cases.AGENT_MESSAGE_ID, "final text"),
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(
            trajectory.steps,
            (_usage.TrajectoryStep(kind=_usage_cases.ASSISTANT_MESSAGE_STEP, content="final text"),),
        )
        self.assertEqual(trajectory.final_output, "final text")

    def test_skips_empty_or_nonstring_message(self) -> None:
        # An empty / non-string agent_message text creates no turn.
        stdout = _jsonl.jsonl(
            _codex.agent_message(_usage_cases.AGENT_MESSAGE_ID, ""),
            _codex.agent_message("a2", 7),
        )
        self.assertEqual(_usage.parse_codex_trajectory(stdout).steps, ())

    def test_started_command_emits_call_no_result(self) -> None:
        # A command that never completes (no aggregated_output) is a call with
        # no result step rather than a fabricated empty result.
        stdout = _jsonl.jsonl(
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                "/bin/bash -lc 'sleep 99'",
                started=True,
                status=_usage_cases.IN_PROGRESS_STATUS,
            ),
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(len(trajectory.steps), 1)
        self.assertEqual(trajectory.steps[0].kind, _usage_cases.TOOL_CALL_STEP)
        self.assertEqual(trajectory.steps[0].tool_id, _usage_cases.ITEM_ONE_ID)
