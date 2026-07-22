# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics trajectory budget tests."""

import json


import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_text as _read_text,
    read_records as _read_records,
)


from tests.analytics_trajectory_cases import (
    claude_multistep_stdout as _claude_multistep_stdout,
)


_TURN_KEY = "turn"


_BASH_TOOL_NAME = "Bash"


_TOOL_CALL_KIND = "tool_call"


_TURNS_KEY = "turns"


_TOOL_RESULT_KIND = "tool_result"


_TRAJECTORY_FILENAME = "trajectory.jsonl"


_PROMPT_TEXT = "p"


_NAME_KEY = "name"


_KIND_KEY = "kind"


_EVENT_KEY = "event"


_CONTENT_KEY = "content"


_ANALYTICS_FILENAME_ALTERNATE = "a.jsonl"


_DROPS_EXCESS_STEPS_OBJECT_ARGUMENT = 2000


_DROPS_EXCESS_STEPS_RESULT_TEXT = 20


AGENT_EXIT_ISSUE_NUMBER = 7


CLAUDE_TRAJECTORY_INPUT_TOKENS = 100


CLAUDE_TRAJECTORY_OUTPUT_TOKENS = 50


TRAJECTORY_REVIEW_ROUND = 2


TRAJECTORY_RETRY_COUNT = 1


_BUDGET_TOOL_PAIR_COUNT = 5


_MANY_TURNS_COUNT = 5_000


_METADATA_ONLY_STEP_COUNT = 10_000


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


def _emit_stubbed_trajectory(test_case, analytics, trajectory) -> tuple[str, dict]:
    with tempfile.TemporaryDirectory() as temp_dir:
        trajectory_path = Path(temp_dir) / _TRAJECTORY_FILENAME
        with patch.object(
            analytics.usage,
            "parse_agent_trajectory",
            return_value=trajectory,
        ):
            test_case._emit(
                analytics,
                stdout="",
                prompt=_PROMPT_TEXT,
                traj_path=trajectory_path,
                analytics_path=Path(temp_dir) / _ANALYTICS_FILENAME_ALTERNATE,
            )
        raw_record = _read_text(trajectory_path)
    return raw_record, json.loads(raw_record)


class RecordAgentExitTrajectoryBudgetTest(_RecordAgentExitTrajectorySupport):
    def test_total_record_budget_drops_excess_steps(self) -> None:
        # When the cumulative redacted content crosses the record budget the
        # remaining steps are dropped and `truncated` is set, so one runaway
        # run cannot write an unbounded JSONL line.
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(analytics, "_TRAJECTORY_RECORD_BUDGET", _DROPS_EXCESS_STEPS_OBJECT_ARGUMENT),
        ):
            t_path = Path(td) / _TRAJECTORY_FILENAME
            self._emit(
                analytics,
                stdout=_claude_multistep_stdout(
                    n_steps=_BUDGET_TOOL_PAIR_COUNT,
                    result_text="ten-chars!" * _DROPS_EXCESS_STEPS_RESULT_TEXT,
                ),
                traj_path=t_path,
                analytics_path=Path(td) / _ANALYTICS_FILENAME_ALTERNATE,
            )
            rec = _read_records(t_path)[0]
            self.assertTrue(rec[_TRUNCATED])
            # 5 pairs => 10 steps emitted; the budget dropped the tail but
            # kept a prefix.
            self.assertGreater(len(rec[_STEPS]), 0)
            self.assertLess(
                len(rec[_STEPS]),
                _BUDGET_TOOL_PAIR_COUNT * 2,
            )
            # The 5 small per-turn entries fit under the budget (they are drawn
            # down before the steps), so all are kept while the step tail is
            # dropped; a turns array that itself overflows is truncated too
            # (see test_turns_array_respects_total_budget).
            self.assertEqual(
                len(rec[_TURNS_KEY]),
                _BUDGET_TOOL_PAIR_COUNT,
            )
            self.assertIn(_RUN_USAGE, rec)

    def test_turns_array_respects_total_budget(self) -> None:
        # Regression: the per-turn `turns[]` array is charged AND truncated
        # under the record budget, not merely charged. A claude run with
        # thousands of turns but no steps would otherwise write the whole
        # array in full via `build_record` and overshoot the budget by its
        # size -- the reviewer reproduced ~914 KB with zero steps kept.
        _, analytics = _reload()
        many = analytics.usage.AgentTrajectory(
            backend=_CLAUDE,
            turns=tuple(
                analytics.usage.TurnUsage(
                    turn=index,
                    model=_CLAUDE_MODEL,
                    input_tokens=1,
                    output_tokens=1,
                )
                for index in range(_MANY_TURNS_COUNT)
            ),
        )
        raw, rec = _emit_stubbed_trajectory(self, analytics, many)
        self.assertTrue(rec[_TRUNCATED])
        self.assertLess(len(rec[_TURNS_KEY]), _MANY_TURNS_COUNT)
        # The on-disk line is bounded near the budget, not the ~914 KB an
        # uncapped turns array produced.
        self.assertLess(len(raw), analytics._TRAJECTORY_RECORD_BUDGET * 2)

    def test_metadata_only_steps_respect_total_budget(self) -> None:
        # Regression: the budget must count each step's serialized metadata,
        # not just `len(content)`. A run of 10,000 empty-content steps -- each
        # still ~80 bytes of `kind` / `name` / `tool_id` JSON -- would
        # otherwise produce a multi-hundred-KB record with NO `truncated`
        # flag, because the old content-length-only check never advanced.
        _, analytics = _reload()
        many = analytics.usage.AgentTrajectory(
            backend=_CLAUDE,
            steps=tuple(
                analytics.usage.TrajectoryStep(
                    kind=_TOOL_CALL_KIND,
                    name="command_execution",
                    tool_id=f"id{index}",
                    content=None,
                )
                for index in range(_METADATA_ONLY_STEP_COUNT)
            ),
        )
        raw, rec = _emit_stubbed_trajectory(self, analytics, many)
        self.assertTrue(rec[_TRUNCATED])
        self.assertLess(
            len(rec[_STEPS]),
            _METADATA_ONLY_STEP_COUNT,
        )
        # The on-disk line is bounded near the budget, not the ~749 KB an
        # uncapped run produced -- one step of overshoot plus the envelope.
        self.assertLess(len(raw), analytics._TRAJECTORY_RECORD_BUDGET * 2)
