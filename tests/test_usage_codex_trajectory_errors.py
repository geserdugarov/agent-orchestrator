# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex malformed and partial trajectory tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexTrajectoryErrorTest(unittest.TestCase):
    """``_usage.parse_codex_trajectory`` over synthetic ``codex exec --json`` runs.

    Codex's tool surface is the shell: each ``command_execution`` is one call
    (its ``command``) plus one result (its ``aggregated_output``), deduped by
    the shared ``item.id`` across the started/completed pair; each
    ``agent_message`` is one ``assistant_message`` text turn (its ``text``),
    captured in stream order. The last ``agent_message`` ``text`` is also the
    final output; ``tools`` / ``system_prompt`` stay empty (no confirmed codex
    frame exposes them).
    """

    def test_null_aggregate_still_emits_result(self) -> None:
        # A completed command whose ``aggregated_output`` is present but null
        # still emits a tool_result step (content None): the recorded-output
        # decision is membership, not truthiness, so a null result is kept.
        stdout = _jsonl.jsonl(
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                "/bin/bash -lc 'true'",
                status=_usage_cases.COMPLETED_STATUS,
                aggregated_output=None,
            ),
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(
            [step.kind for step in trajectory.steps],
            [_usage_cases.TOOL_CALL_STEP, _usage_cases.TOOL_RESULT_STEP],
        )
        self.assertIsNone(trajectory.steps[1].content)

    def test_missing_fields_yield_empty_sections(self) -> None:
        stdout = _jsonl.jsonl(
            {_usage_cases.TYPE_FIELD: _usage_cases.THREAD_STARTED_EVENT},
            {
                _usage_cases.TYPE_FIELD: _usage_cases.TURN_COMPLETED_EVENT,
                _usage_cases.USAGE_FIELD: {_usage_cases.INPUT_TOKENS_FIELD: 1},
            },
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.steps, ())
        self.assertIsNone(trajectory.final_output)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.skills, _usage.SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                _usage_cases.SHELL_LIST_COMMAND,
                status=_usage_cases.COMPLETED_STATUS,
                aggregated_output=_usage_cases.COMMAND_OUTPUT,
            )
        )
        stdout = "\n".join(
            [
                "codex starting...",
                '{"truncated":',
                good,
                "trailing-noise",
            ]
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(
            [step.kind for step in trajectory.steps], [_usage_cases.TOOL_CALL_STEP, _usage_cases.TOOL_RESULT_STEP]
        )

    def test_has_no_per_turn_usage(self) -> None:
        # Codex usage frames are cumulative, not per-turn, so the per-turn
        # section stays empty and no step is stamped with a turn index -- the
        # run-level summary is codex's only usage surface (mirrors how tools /
        # skills_available stay best-effort-empty for codex).
        stdout = _jsonl.jsonl(
            _codex.command(
                _usage_cases.ITEM_ONE_ID,
                _usage_cases.SHELL_LIST_COMMAND,
                status=_usage_cases.COMPLETED_STATUS,
                aggregated_output=_usage_cases.COMMAND_OUTPUT,
            ),
            _codex.agent_message(_usage_cases.AGENT_MESSAGE_ID, _usage_cases.FINAL_OUTPUT),
            {
                _usage_cases.TYPE_FIELD: _usage_cases.TURN_COMPLETED_EVENT,
                _usage_cases.USAGE_FIELD: {
                    _usage_cases.INPUT_TOKENS_FIELD: 10,
                    _usage_cases.OUTPUT_TOKENS_FIELD: 5,
                },
            },
        )
        trajectory = _usage.parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.turns, ())
        self.assertTrue(trajectory.steps)
        self.assertTrue(all(step.turn is None for step in trajectory.steps))

    def test_empty_stdout(self) -> None:
        self.assertEqual(_usage.parse_codex_trajectory(""), _usage.AgentTrajectory(backend=_usage_cases.CODEX))
