# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics trajectory core recording tests."""

import json


import os


import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_records as _read_records,
)


from tests.analytics_trajectory_cases import (
    claude_trajectory_stdout as _claude_trajectory_stdout,
    codex_trajectory_stdout as _codex_trajectory_stdout,
)


_TURN_KEY = "turn"


_BASH_TOOL_NAME = "Bash"


_TOOL_CALL_KIND = "tool_call"


_TURNS_KEY = "turns"


_TOOL_RESULT_KIND = "tool_result"


_TYPE_KEY = "type"


_TRAJECTORY_FILENAME = "trajectory.jsonl"


_PROMPT_TEXT = "p"


_COMMAND_KEY = "command"


_ANALYTICS_FILENAME = "analytics.jsonl"


_NAME_KEY = "name"


_REDACTION_MARKER = "***"


_KIND_KEY = "kind"


_EVENT_KEY = "event"


_CONTENT_KEY = "content"


_TEXT_KEY = "text"


_ANALYTICS_FILENAME_ALTERNATE = "a.jsonl"


AGENT_EXIT_ISSUE_NUMBER = 7


CLAUDE_TRAJECTORY_INPUT_TOKENS = 100


CLAUDE_TRAJECTORY_OUTPUT_TOKENS = 50


CODEX_TRAJECTORY_INPUT_TOKENS = 200


CODEX_TRAJECTORY_OUTPUT_TOKENS = 80


TRAJECTORY_REVIEW_ROUND = 2


TRAJECTORY_RETRY_COUNT = 1


_TRUNCATION_EDGE_CHARS = 5


_LONG_TEXT_CHARS = 100


_REPO = "owner/repo"


_CLAUDE = "claude"


_CODEX = "codex"


_STAGE_IMPLEMENTING = "implementing"


_DEVELOPER = "developer"


_AGENT_EXIT = "agent_exit"


_AGENT_TRAJECTORY = "agent_trajectory"


_CLAUDE_MODEL = "claude-sonnet-4-6"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


_INPUT_TOKENS = "input_tokens"


_OUTPUT_TOKENS = "output_tokens"


_STEPS = "steps"


_BACKEND = "backend"


_OUTPUT = "output"


_RUN_USAGE = "run_usage"


_USER_INPUT = "user_input"


_TRUNCATED = "truncated"


@dataclass(frozen=True)
class _TrajectoryExitCase:
    stdout: str
    prompt: str | None = None
    traj_path: Path | None = None
    analytics_path: Path | None = None
    backend: str = _CLAUDE
    track: bool = False


class _RecordAgentExitTrajectorySupport(unittest.TestCase):
    """`record_agent_exit` writes the opt-in trajectory record only when
    `TRAJECTORY_LOG_PATH` is enabled, redacts every free-text field, applies
    head/tail + total-size truncation caps, and never lets a trajectory
    failure drop the baseline `agent_exit` usage record."""

    def _emit(self, analytics, **options):
        case = _TrajectoryExitCase(**options)
        with (
            patch.object(analytics, _ANALYTICS_LOG_PATH, case.analytics_path),
            patch.object(analytics, _TRAJECTORY_LOG_PATH, case.traj_path),
            patch.object(analytics, _TRACK_SKILL_TRIGGERS, case.track),
        ):
            return analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=case.backend,
                agent_spec=case.backend,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess-traj",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=case.stdout,
                    stderr="",
                ),
                duration_s=float(),
                review_round=TRAJECTORY_REVIEW_ROUND,
                retry_count=TRAJECTORY_RETRY_COUNT,
                prompt=case.prompt,
            )

    def _assert_baseline_exit_record(self, path: Path) -> None:
        records = _read_records(path)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record[_EVENT_KEY], _AGENT_EXIT)
        self.assertEqual(
            record[_INPUT_TOKENS],
            CLAUDE_TRAJECTORY_INPUT_TOKENS,
        )
        self.assertNotIn(_USER_INPUT, record)
        self.assertNotIn(_RUN_USAGE, record)

    def _read_single_trajectory(self, path: Path) -> dict:
        records = _read_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][_EVENT_KEY], _AGENT_TRAJECTORY)
        return records[0]

    def _assert_claude_trajectory_identity(self, record: dict) -> None:
        expected = {
            _EVENT_KEY: _AGENT_TRAJECTORY,
            "repo": _REPO,
            "issue": AGENT_EXIT_ISSUE_NUMBER,
            "stage": _STAGE_IMPLEMENTING,
            "agent_role": _DEVELOPER,
            _BACKEND: _CLAUDE,
            "session_id": "sess-traj",
            "review_round": TRAJECTORY_REVIEW_ROUND,
            "retry_count": TRAJECTORY_RETRY_COUNT,
            _USER_INPUT: "implement X",
            "tools": ["Read", _BASH_TOOL_NAME],
            _OUTPUT: "implemented",
        }
        self.assertEqual(
            {key: record[key] for key in expected},
            expected,
        )

    def _assert_claude_trajectory_steps(self, record: dict) -> None:
        steps = record[_STEPS]
        tool_call = steps[0]
        self.assertEqual(
            {
                "kinds": [step[_KIND_KEY] for step in steps],
                "tool_name": tool_call[_NAME_KEY],
                _TOOL_RESULT_KIND: steps[1][_CONTENT_KEY],
                "tool_turn": tool_call[_TURN_KEY],
            },
            {
                "kinds": [_TOOL_CALL_KIND, _TOOL_RESULT_KIND],
                "tool_name": _BASH_TOOL_NAME,
                _TOOL_RESULT_KIND: "hi",
                "tool_turn": 0,
            },
        )
        self.assertIn("echo hi", tool_call[_CONTENT_KEY])
        # Tool results become the next turn's input; only the billed call
        # carries the current turn index.
        self.assertNotIn(_TURN_KEY, steps[1])

    def _assert_claude_trajectory_usage(self, record: dict) -> None:
        run_usage = record[_RUN_USAGE]
        expected_run = {
            _INPUT_TOKENS: CLAUDE_TRAJECTORY_INPUT_TOKENS,
            _OUTPUT_TOKENS: CLAUDE_TRAJECTORY_OUTPUT_TOKENS,
            "models": [_CLAUDE_MODEL],
            _TURNS_KEY: 1,
            "cost_source": "estimated",
        }
        self.assertNotIn(_BACKEND, run_usage)
        self.assertEqual(
            {key: run_usage[key] for key in expected_run},
            expected_run,
        )

        turns = record[_TURNS_KEY]
        expected_turn = {
            _TURN_KEY: 0,
            "model": _CLAUDE_MODEL,
            _INPUT_TOKENS: CLAUDE_TRAJECTORY_INPUT_TOKENS,
            _OUTPUT_TOKENS: CLAUDE_TRAJECTORY_OUTPUT_TOKENS,
            "cost_source": "estimated",
        }
        self.assertEqual(len(turns), 1)
        self.assertEqual(
            {key: turns[0][key] for key in expected_turn},
            expected_turn,
        )


def _discover_codex_tools() -> list[str]:
    from orchestrator import skill_catalog

    return list(skill_catalog.discover_codex_tools())


def _codex_usage_projection(record: dict) -> tuple:
    usage = record[_RUN_USAGE]
    return (
        _BACKEND in usage,
        usage[_INPUT_TOKENS],
        usage[_OUTPUT_TOKENS],
        usage["cost_source"],
        usage["cost_usd"],
    )


def _text_turn_stdout(secret: str) -> str:
    frames = [
        {_TYPE_KEY: "system", "subtype": "init", "tools": [_BASH_TOOL_NAME]},
        {
            _TYPE_KEY: "assistant",
            "message": {
                "id": "m1",
                _CONTENT_KEY: [
                    {_TYPE_KEY: _TEXT_KEY, _TEXT_KEY: "B" * _LONG_TEXT_CHARS},
                    {
                        _TYPE_KEY: "tool_use",
                        _NAME_KEY: _BASH_TOOL_NAME,
                        "id": "tu1",
                        "input": {_COMMAND_KEY: "ls"},
                    },
                ],
            },
        },
        {
            _TYPE_KEY: "user",
            "message": {
                _CONTENT_KEY: [
                    {_TYPE_KEY: _TOOL_RESULT_KIND, "tool_use_id": "tu1", _CONTENT_KEY: "ok"},
                    {_TYPE_KEY: _TEXT_KEY, _TEXT_KEY: f"leak {secret}"},
                ]
            },
        },
        {_TYPE_KEY: "result", "result": "done"},
    ]
    return "\n".join(json.dumps(frame) for frame in frames)


class RecordAgentExitTrajectoryCoreTest(_RecordAgentExitTrajectorySupport):
    def test_sink_off_writes_no_trajectory_or_input(self) -> None:
        # Default off: a prompt is passed but, with the trajectory sink
        # disabled, no trajectory file is created and the baseline
        # `agent_exit` record never carries `user_input`.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            a_path = temp_root / _ANALYTICS_FILENAME
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(),
                prompt="please implement the feature",
                traj_path=None,
                analytics_path=a_path,
            )
            # Only the analytics file exists -- no trajectory file anywhere.
            self.assertEqual(
                sorted(entry.name for entry in temp_root.iterdir()),
                [_ANALYTICS_FILENAME],
            )
            recs = _read_records(a_path)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0][_EVENT_KEY], _AGENT_EXIT)
            self.assertNotIn(_USER_INPUT, recs[0])

    def test_sink_on_writes_redacted_trajectory(self) -> None:
        # Sink on: a single `agent_trajectory` record carries the redacted
        # user_input, the offered tools, the ordered steps with their
        # tool_call input / tool_result content, and the final output --
        # alongside (not replacing) the baseline `agent_exit` record.
        analytics = _reload()[1]
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / _ANALYTICS_FILENAME
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={_COMMAND_KEY: "echo hi"},
                    tool_result="hi",
                    final_output="implemented",
                ),
                prompt="implement X",
                traj_path=t_path,
                analytics_path=a_path,
            )
            self._assert_baseline_exit_record(a_path)
            record = self._read_single_trajectory(t_path)
            self._assert_claude_trajectory_identity(record)
            self._assert_claude_trajectory_steps(record)
            self._assert_claude_trajectory_usage(record)
            self.assertNotIn(_TRUNCATED, record)

    def test_codex_trajectory_record(self) -> None:
        # The codex backend dispatches through the same path: command +
        # aggregated_output become the tool_call / tool_result, the trailing
        # agent_message rides along as an assistant_message turn, and that
        # same last agent_message is the output.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_codex_trajectory_stdout(),
                prompt="codex prompt",
                traj_path=t_path,
                analytics_path=t_path.parent / _ANALYTICS_FILENAME_ALTERNATE,
                backend=_CODEX,
            )
            rec = _read_records(t_path)[0]
            steps = rec[_STEPS]
            self.assertEqual(
                (
                    rec[_EVENT_KEY],
                    rec[_BACKEND],
                    rec[_USER_INPUT],
                    rec[_OUTPUT],
                ),
                (_AGENT_TRAJECTORY, _CODEX, "codex prompt", "codex done"),
            )
            self.assertEqual(
                [
                    (step[_KIND_KEY], step[_CONTENT_KEY])
                    for step in steps
                ],
                [
                    (_TOOL_CALL_KIND, "ls -la"),
                    (_TOOL_RESULT_KIND, "command output"),
                    ("assistant_message", "codex done"),
                ],
            )
            # The text turn carries no tool name / id.
            self.assertEqual(
                (steps[2][_NAME_KEY], steps[2]["tool_id"]),
                (None, None),
            )
            # codex exposes no offered-tools frame, so the trajectory record
            # backfills the best-effort baseline out-of-band.
            self.assertEqual(rec["tools"], _discover_codex_tools())
            # run_usage is codex's only usage surface: the denormalized
            # run-level totals, present even though per-turn detail is not.
            self.assertEqual(
                _codex_usage_projection(rec),
                (
                    False,
                    CODEX_TRAJECTORY_INPUT_TOKENS,
                    CODEX_TRAJECTORY_OUTPUT_TOKENS,
                    "unknown-price",
                    None,
                ),
            )
            # No priced model in the stream -> unknown-price, no cost.
            # codex usage frames are cumulative, not per-turn: the per-turn
            # array is dropped and no step carries a `turn` index.
            self.assertNotIn(_TURNS_KEY, rec)
            self.assertTrue(all(_TURN_KEY not in step for step in steps))

    def test_text_turns_redacted_capped_and_recorded(self) -> None:
        # New timeline items -- assistant / user text turns -- are stored as
        # their own steps and get the same treatment as tool payloads: stream
        # order preserved, secrets masked, over-long text head/tail truncated,
        # and `name` / `tool_id` null (text turns carry no tool metadata).
        _, analytics = _reload()
        secret = "sk-ant-TEXTLEAK-0123456789"
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}),
            patch.object(
                analytics,
                "_TRAJECTORY_FIELD_HEAD",
                _TRUNCATION_EDGE_CHARS,
            ),
            patch.object(
                analytics,
                "_TRAJECTORY_FIELD_TAIL",
                _TRUNCATION_EDGE_CHARS,
            ),
        ):
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_text_turn_stdout(secret),
                prompt=_PROMPT_TEXT,
                traj_path=t_path,
                analytics_path=t_path.parent / _ANALYTICS_FILENAME_ALTERNATE,
            )
            steps = _read_records(t_path)[0][_STEPS]
            self.assertEqual(
                [step[_KIND_KEY] for step in steps],
                ["assistant_message", _TOOL_CALL_KIND, _TOOL_RESULT_KIND, "user_message"],
            )
            # Long assistant text head/tail truncated; no tool metadata.
            self.assertLess(
                len(steps[0][_CONTENT_KEY]),
                _LONG_TEXT_CHARS,
            )
            self.assertIn("chars elided", steps[0][_CONTENT_KEY])
            self.assertIsNone(steps[0][_NAME_KEY])
            self.assertIsNone(steps[0]["tool_id"])
            # Secret masked in the user text turn and nowhere survives.
            self.assertEqual(steps[3][_KIND_KEY], "user_message")
            self.assertIn(_REDACTION_MARKER, steps[3][_CONTENT_KEY])
            self.assertNotIn(secret, json.dumps(_read_records(t_path)[0]))
