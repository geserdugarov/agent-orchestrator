# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory recording and sink isolation for agent runs."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from orchestrator import analytics, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient

from tests import workflow_agent_analytics_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
EVENT_AGENT_EXIT = support.EVENT_AGENT_EXIT
EVENT_AGENT_TRAJECTORY = support.EVENT_AGENT_TRAJECTORY
EVENT_SKILL_TRIGGERED = support.EVENT_SKILL_TRIGGERED
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
ROLE_DEVELOPER = support.ROLE_DEVELOPER
_AGENT_ROLE_KEY = support._AGENT_ROLE_KEY
_ANALYTICS_FILENAME = support._ANALYTICS_FILENAME
_ANALYTICS_PATH_ATTR = support._ANALYTICS_PATH_ATTR
_CLAUDE_MODEL = support._CLAUDE_MODEL
_CONTENT_KEY = support._CONTENT_KEY
_DEVELOP_SKILL = support._DEVELOP_SKILL
_EVENT_KEY = support._EVENT_KEY
_FAKE_WT = support._FAKE_WT
_ID_KEY = support._ID_KEY
_INPUT_TOKENS_KEY = support._INPUT_TOKENS_KEY
_MESSAGE_KEY = support._MESSAGE_KEY
_OUTPUT_TOKENS_KEY = support._OUTPUT_TOKENS_KEY
_PROMPT_FORWARDING_ISSUE_NUMBER = support._PROMPT_FORWARDING_ISSUE_NUMBER
_RESULT_KEY = support._RESULT_KEY
_RUN_AGENT_ATTR = support._RUN_AGENT_ATTR
_SKILL_KEY = support._SKILL_KEY
_STAGE_KEY = support._STAGE_KEY
_TRACK_SKILLS_ATTR = support._TRACK_SKILLS_ATTR
_TRAJECTORY_FAILURE_ISSUE_NUMBER = support._TRAJECTORY_FAILURE_ISSUE_NUMBER
_TRAJECTORY_ISSUE_NUMBER = support._TRAJECTORY_ISSUE_NUMBER
_TRAJECTORY_PATH_ATTR = support._TRAJECTORY_PATH_ATTR
_TRAJECTORY_PROMPT = support._TRAJECTORY_PROMPT
_TRAJECTORY_SINK_ISSUE_NUMBER = support._TRAJECTORY_SINK_ISSUE_NUMBER
_TYPE_KEY = support._TYPE_KEY
_USAGE_KEY = support._USAGE_KEY
_analytics_path = support._analytics_path
_analytics_records = support._analytics_records
_claude_stdout_with_skills = support._claude_stdout_with_skills


def _claude_trajectory_stdout(
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    tool_result: str = "result text",
    final_output: str = "final answer",
) -> str:
    """A claude stream-json stdout with one tool_use / tool_result step, a
    usage block, and a terminal `result` answer -- the surface the
    trajectory classifier reconstructs."""
    frames = [
        {_TYPE_KEY: "system", "subtype": "init", "tools": ["Read", "Bash"]},
        {
            _TYPE_KEY: "assistant",
            _MESSAGE_KEY: {
                _ID_KEY: "m1", "model": _CLAUDE_MODEL,
                _CONTENT_KEY: [{
                    _TYPE_KEY: "tool_use", "name": tool_name, _ID_KEY: "tu1",
                    "input": tool_input or {"command": "ls"},
                }],
                _USAGE_KEY: {_INPUT_TOKENS_KEY: 100, _OUTPUT_TOKENS_KEY: 50},
            },
        },
        {
            _TYPE_KEY: "user",
            _MESSAGE_KEY: {_CONTENT_KEY: [{
                _TYPE_KEY: "tool_result", "tool_use_id": "tu1",
                _CONTENT_KEY: tool_result,
            }]},
        },
        {_TYPE_KEY: _RESULT_KEY, "num_turns": 1, _RESULT_KEY: final_output},
    ]
    return "\n".join(json.dumps(frame) for frame in frames)


def _assert_trajectory_record(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_EVENT_KEY], EVENT_AGENT_TRAJECTORY)
    case.assertEqual(record["issue"], _TRAJECTORY_ISSUE_NUMBER)
    case.assertEqual(record[_STAGE_KEY], LABEL_IMPLEMENTING)
    case.assertEqual(record[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
    case.assertEqual(record["user_input"], _TRAJECTORY_PROMPT)
    case.assertEqual(record["output"], "implemented")
    case.assertEqual(
        [step["kind"] for step in record["steps"]],
        ["tool_call", "tool_result"],
    )


def _assert_base_record(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_EVENT_KEY], EVENT_AGENT_EXIT)
    case.assertNotIn("user_input", record)


def _run_trajectory(
    *,
    stdout: str,
    prompt: str,
    trajectory_path: Optional[Path],
    analytics_path: Optional[Path] = None,
) -> AgentResult:
    gh = FakeGitHubClient()
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(analytics, _TRAJECTORY_PATH_ATTR, trajectory_path), \
            patch.object(analytics, _TRACK_SKILLS_ATTR, False), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-traj",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )
        return workflow._run_agent_tracked(
            gh, _TRAJECTORY_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=BACKEND_CLAUDE,
            prompt=prompt,
            cwd=_FAKE_WT,
            agent_spec=BACKEND_CLAUDE,
            review_round=2,
            retry_count=1,
        )


class TrajectoryRecordingTest(unittest.TestCase):
    """`_run_agent_tracked` forwards its prompt to `record_agent_exit`, which
    writes one redacted `agent_trajectory` record only when
    `TRAJECTORY_LOG_PATH` is enabled -- never disturbing the baseline
    `agent_exit` analytics record or the `skill_triggered` audit events."""

    def test_prompt_is_forwarded_to_record_agent_exit(self) -> None:
        # The orchestrator-built prompt reaches `record_agent_exit` as the
        # `prompt` kwarg -- the seam that lets it become `user_input`.
        gh = FakeGitHubClient()
        record_mock = MagicMock(return_value=None)
        with patch.object(
            analytics, "record_agent_exit", record_mock,
        ), patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
            run_mock.return_value = AgentResult(
                session_id="s", last_message="", exit_code=0,
                timed_out=False, stdout="", stderr="",
            )
            workflow._run_agent_tracked(
                gh, _PROMPT_FORWARDING_ISSUE_NUMBER,
                agent_role=ROLE_DEVELOPER,
                stage=LABEL_IMPLEMENTING,
                backend=BACKEND_CLAUDE,
                prompt="PROMPT-MARKER-XYZ",
                cwd=_FAKE_WT,
            )
        self.assertEqual(record_mock.call_count, 1)
        self.assertEqual(
            record_mock.call_args.kwargs["prompt"],
            "PROMPT-MARKER-XYZ",
        )

    def test_redacts_user_input(self) -> None:
        trajectory_path = _analytics_path(
            self,
            "traj-on-",
            "trajectory.jsonl",
        )
        analytics_path = _analytics_path(self, "traj-analytics-")
        _run_trajectory(
            stdout=_claude_trajectory_stdout(
                tool_result="hi",
                final_output="implemented",
            ),
            prompt=_TRAJECTORY_PROMPT,
            trajectory_path=trajectory_path,
            analytics_path=analytics_path,
        )
        trajectory_records = _analytics_records(trajectory_path)
        self.assertEqual(len(trajectory_records), 1)
        _assert_trajectory_record(self, trajectory_records[0])
        base_records = _analytics_records(analytics_path)
        self.assertEqual(len(base_records), 1)
        _assert_base_record(self, base_records[0])

    def test_no_trajectory_record_when_sink_off(self) -> None:
        # Default off: a prompt is passed but no trajectory file is created;
        # the baseline agent_exit record is still written.
        with tempfile.TemporaryDirectory(prefix="traj-off-") as td:
            a_path = Path(td) / _ANALYTICS_FILENAME
            _run_trajectory(
                stdout=_claude_trajectory_stdout(),
                prompt=_TRAJECTORY_PROMPT,
                trajectory_path=None,
                analytics_path=a_path,
            )
            self.assertEqual(
                sorted(path.name for path in Path(td).iterdir()),
                [_ANALYTICS_FILENAME],
            )
            base = _analytics_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertNotIn("user_input", base[0])

    def test_failure_keeps_skill_events(self) -> None:
        # A trajectory-parse failure must not cost the `skill_triggered`
        # audit events: they are driven by the value `record_agent_exit`
        # returns before the trajectory block runs.
        gh = FakeGitHubClient()
        with tempfile.TemporaryDirectory(prefix="traj-failopen-") as td:
            t_path = Path(td) / "trajectory.jsonl"
            with (
                patch.object(analytics, _ANALYTICS_PATH_ATTR, None),
                patch.object(analytics, _TRAJECTORY_PATH_ATTR, t_path),
                patch.object(analytics, _TRACK_SKILLS_ATTR, True),
                patch.object(
                    analytics.usage,
                    "parse_agent_trajectory",
                    side_effect=RuntimeError("boom"),
                ),
                patch.object(workflow, _RUN_AGENT_ATTR) as run_mock,
                self.assertLogs(analytics.log, level="ERROR"),
            ):
                run_mock.return_value = AgentResult(
                    session_id="sess-skill", last_message="", exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout_with_skills(skills=(_DEVELOP_SKILL,)),
                    stderr="",
                )
                workflow._run_agent_tracked(
                    gh, _TRAJECTORY_FAILURE_ISSUE_NUMBER,
                    agent_role=ROLE_DEVELOPER,
                    stage=LABEL_IMPLEMENTING,
                    backend=BACKEND_CLAUDE,
                    prompt="p",
                    cwd=_FAKE_WT,
                    agent_spec=BACKEND_CLAUDE,
                )
            skill_events = [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_SKILL_TRIGGERED
            ]
            self.assertEqual(
                [event[_SKILL_KEY] for event in skill_events],
                [_DEVELOP_SKILL],
            )
            self.assertFalse(t_path.exists())


def _drive_trajectory_sink(
    gh: FakeGitHubClient,
    *,
    analytics_path: Path,
) -> None:
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-traj-guard",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=_claude_trajectory_stdout(
                tool_result="hi", final_output="done",
            ),
            stderr="",
        )
        workflow._run_agent_tracked(
            gh, _TRAJECTORY_SINK_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=BACKEND_CLAUDE,
            prompt=_TRAJECTORY_PROMPT,
            cwd=_FAKE_WT,
        )


class TrajectorySinkHermeticityTest(unittest.TestCase):
    """Regression guard for the hermetic test default: the global conftest
    fixture pins `TRAJECTORY_LOG_PATH` to None so a workflow analytics path
    can never write to an operator-configured trajectory sink -- even though
    the same synthetic run writes one record when the sink is left on."""

    def test_global_fixture_pins_trajectory_sink_off(self) -> None:
        # The autouse conftest fixture neutralizes any operator-exported
        # TRAJECTORY_LOG_PATH for the duration of every test.
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_pinned_off_sink_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="traj-guard-") as td:
            configured = Path(td) / "operator-trajectories.jsonl"
            a_path = Path(td) / _ANALYTICS_FILENAME

            # Stand up an operator-configured sink, live on the analytics
            # module exactly as an exported TRAJECTORY_LOG_PATH resolves at
            # import. It stays configured for the whole test so the suppressed
            # run below is gated by the off-pin, not by an ambient None that a
            # bare runner (no env var) would also exhibit.
            with patch.object(analytics, _TRAJECTORY_PATH_ATTR, configured):
                # Control: the configured sink genuinely captures the synthetic
                # run, so the suppression asserted below is a real effect.
                _drive_trajectory_sink(
                    FakeGitHubClient(),
                    analytics_path=a_path,
                )
                self.assertTrue(configured.exists())
                configured.unlink()

                # The documented "off" knob (None) -- the value the conftest
                # autouse fixture installs for every test -- must suppress the
                # write to that still-configured path. Cover both halves of
                # "not created or appended", while the baseline agent_exit
                # record is still produced.
                with patch.object(analytics, _TRAJECTORY_PATH_ATTR, None):
                    # (a) not created: an absent sink stays absent.
                    _drive_trajectory_sink(
                        FakeGitHubClient(),
                        analytics_path=a_path,
                    )
                    self.assertFalse(
                        configured.exists(),
                        "trajectory sink created the operator-configured "
                        "path while pinned off",
                    )
                    # (b) not appended: a pre-existing sink is left byte-for-
                    # byte unchanged.
                    sentinel = '{"event": "pre-existing"}\n'
                    configured.write_text(sentinel, encoding="utf-8")
                    _drive_trajectory_sink(
                        FakeGitHubClient(),
                        analytics_path=a_path,
                    )
                    self.assertEqual(
                        configured.read_text(encoding="utf-8"), sentinel,
                        "trajectory sink appended to the operator-configured "
                        "path while pinned off",
                    )
                self.assertTrue(a_path.exists())
