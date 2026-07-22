# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics trajectory redaction tests."""

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


TRAJECTORY_REVIEW_ROUND = 2


TRAJECTORY_RETRY_COUNT = 1


_TRUNCATION_EDGE_CHARS = 5


_LONG_TEXT_CHARS = 100


_REPO = "owner/repo"


_CLAUDE = "claude"


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


def _tool_result_body(record: dict) -> str:
    result_step = next(
        step
        for step in record[_STEPS]
        if step[_KIND_KEY] == _TOOL_RESULT_KIND
    )
    return result_step[_CONTENT_KEY]


class RecordAgentExitTrajectoryRedactionTest(_RecordAgentExitTrajectorySupport):
    def test_secrets_redacted_in_every_field(self) -> None:
        # The secret env value must not survive in user_input, the tool_call
        # input, the tool_result content, or the output. `_redact_secrets`
        # reads the live os.environ, so set a secret-shaped var around the
        # call and assert it is masked everywhere.
        _, analytics = _reload()
        secret = "sk-ant-DEADBEEF-secret-value-0123456789"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={_COMMAND_KEY: f"echo {secret}"},
                    tool_result=f"leaked {secret} here",
                    final_output=f"the answer is {secret}",
                ),
                prompt=f"use token {secret}",
                traj_path=t_path,
                analytics_path=Path(td) / _ANALYTICS_FILENAME_ALTERNATE,
            )
            rec = _read_records(t_path)[0]
            self.assertNotIn(secret, json.dumps(rec))
            # The masking marker landed in each field that carried it.
            self.assertIn(_REDACTION_MARKER, rec[_USER_INPUT])
            self.assertIn(_REDACTION_MARKER, rec[_OUTPUT])
            self.assertIn(_REDACTION_MARKER, rec[_STEPS][0][_CONTENT_KEY])
            self.assertIn(_REDACTION_MARKER, rec[_STEPS][1][_CONTENT_KEY])

    def test_multiline_tool_secret_is_redacted(self) -> None:
        # Regression: dict / list tool payloads are redacted leaf-by-leaf
        # BEFORE JSON serialization. A multiline secret env value would
        # otherwise have its newlines escaped by `json.dumps` (`\n` -> the
        # two-char escape), leaving `_redact_secrets`' literal `str.replace`
        # unable to match the raw value -- so the secret would leak into
        # `steps[].content`. Redacting raw leaves first keeps it masked, for
        # both the dict tool_call input and the list tool_result content.
        _, analytics = _reload()
        secret = "topsecretvalue\nwith-newline-marker-0123456789"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"MULTILINE_SECRET_KEY": secret}):
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={_COMMAND_KEY: f"echo {secret}"},
                    tool_result=[{_TYPE_KEY: _TEXT_KEY, _TEXT_KEY: f"saw {secret}"}],
                    final_output="done",
                ),
                prompt=_PROMPT_TEXT,
                traj_path=t_path,
                analytics_path=Path(td) / _ANALYTICS_FILENAME_ALTERNATE,
            )
            rec = _read_records(t_path)[0]
            # Neither the raw value nor its distinctive post-newline marker
            # survives anywhere in the record.
            self.assertNotIn("with-newline-marker-0123456789", json.dumps(rec))
            self.assertNotIn("topsecretvalue", json.dumps(rec))
            # Both the dict input and the list content carry the mask.
            self.assertIn(_REDACTION_MARKER, rec[_STEPS][0][_CONTENT_KEY])
            self.assertIn(_REDACTION_MARKER, rec[_STEPS][1][_CONTENT_KEY])

    def test_per_step_content_head_tail_truncated(self) -> None:
        # A long field is redacted then truncated to head + tail chars with
        # an elision marker, so a single huge tool output cannot bloat one
        # step. Shrink the caps so the test stays small.
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
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
                stdout=_claude_trajectory_stdout(
                    tool_result="A" * _LONG_TEXT_CHARS,
                    final_output="done",
                ),
                prompt=_PROMPT_TEXT,
                traj_path=t_path,
                analytics_path=Path(td) / _ANALYTICS_FILENAME_ALTERNATE,
            )
            body = _tool_result_body(_read_records(t_path)[0])
            self.assertLess(len(body), _LONG_TEXT_CHARS)
            edge = "A" * _TRUNCATION_EDGE_CHARS
            self.assertTrue(body.startswith(edge))
            self.assertTrue(body.endswith(edge))
            self.assertIn("chars elided", body)
