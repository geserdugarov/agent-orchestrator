# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics trajectory failure-isolation tests."""

import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_records as _read_records,
)


from tests.analytics_recording_cases import (
    claude_stdout_with_skills as _claude_stdout_with_skills,
)


from tests.analytics_trajectory_cases import (
    claude_trajectory_stdout as _claude_trajectory_stdout,
)


_TURN_KEY = "turn"


_BASH_TOOL_NAME = "Bash"


_TOOL_CALL_KIND = "tool_call"


_TURNS_KEY = "turns"


_TOOL_RESULT_KIND = "tool_result"


_TRAJECTORY_FILENAME = "trajectory.jsonl"


_PROMPT_TEXT = "p"


_ANALYTICS_FILENAME = "analytics.jsonl"


_NAME_KEY = "name"


_KIND_KEY = "kind"


_EVENT_KEY = "event"


_CONTENT_KEY = "content"


_ANALYTICS_FILENAME_ALTERNATE = "a.jsonl"


AGENT_EXIT_ISSUE_NUMBER = 7


SKILL_STREAM_INPUT_TOKENS = 1_000


CLAUDE_TRAJECTORY_INPUT_TOKENS = 100


CLAUDE_TRAJECTORY_OUTPUT_TOKENS = 50


TRAJECTORY_REVIEW_ROUND = 2


TRAJECTORY_RETRY_COUNT = 1


_REPO = "owner/repo"


_CLAUDE = "claude"


_DEVELOP = "develop"


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


class RecordAgentExitTrajectoryFailureTest(_RecordAgentExitTrajectorySupport):
    def test_parser_failure_keeps_baseline_and_skills(self) -> None:
        # The trajectory parse rides its own fail-open guard: a parser bug
        # logs and is swallowed, leaving the baseline `agent_exit` record AND
        # the skill-trigger return value (which drives the audit events)
        # intact.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / _ANALYTICS_FILENAME
            t_path = Path(td) / _TRAJECTORY_FILENAME
            with (
                patch.object(
                    analytics.usage,
                    "parse_agent_trajectory",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertLogs(analytics.log, level="ERROR"),
            ):
                self.assertEqual(
                    self._emit(
                        analytics,
                        stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                        prompt=_PROMPT_TEXT,
                        traj_path=t_path,
                        analytics_path=a_path,
                        track=True,
                    ),
                    [_DEVELOP],
                )
            # Skill return value (and thus audit emission) is unaffected.
            # Baseline record survived...
            base = _read_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertEqual(base[0][_EVENT_KEY], _AGENT_EXIT)
            self.assertEqual(
                base[0][_INPUT_TOKENS],
                SKILL_STREAM_INPUT_TOKENS,
            )
            # ...and the broken trajectory wrote nothing.
            self.assertFalse(t_path.exists())

    def test_sink_failure_keeps_baseline_record(self) -> None:
        # A non-OSError escaping the sink append (a programming error past
        # the inner OSError swallow) must not drop the baseline record: the
        # outer guard logs and falls through.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / _ANALYTICS_FILENAME
            with (
                patch.object(
                    analytics,
                    "append_trajectory_record",
                    side_effect=RuntimeError("sink boom"),
                ),
                self.assertLogs(analytics.log, level="ERROR"),
            ):
                self._emit(
                    analytics,
                    stdout=_claude_trajectory_stdout(),
                    prompt=_PROMPT_TEXT,
                    traj_path=Path(td) / _TRAJECTORY_FILENAME,
                    analytics_path=a_path,
                )
            base = _read_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertEqual(base[0][_EVENT_KEY], _AGENT_EXIT)

    def test_absent_prompt_drops_user_input(self) -> None:
        # No prompt passed -> `user_input` is dropped (not stored as null),
        # while the rest of the trajectory still records.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(final_output="x"),
                prompt=None,
                traj_path=t_path,
                analytics_path=Path(td) / _ANALYTICS_FILENAME_ALTERNATE,
            )
            rec = _read_records(t_path)[0]
            self.assertNotIn(_USER_INPUT, rec)
            self.assertEqual(rec[_OUTPUT], "x")
