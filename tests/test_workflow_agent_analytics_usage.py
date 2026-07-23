# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Usage parsing and surfaced tracked-agent metrics."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from orchestrator import analytics, usage, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient

from tests import workflow_agent_analytics_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
EVENT_AGENT_EXIT = support.EVENT_AGENT_EXIT
EVENT_SKILL_TRIGGERED = support.EVENT_SKILL_TRIGGERED
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
ROLE_DEVELOPER = support.ROLE_DEVELOPER
_ANALYTICS_FILENAME = support._ANALYTICS_FILENAME
_ANALYTICS_PATH_ATTR = support._ANALYTICS_PATH_ATTR
_CLAUDE_CACHE_WRITE_TOKENS = support._CLAUDE_CACHE_WRITE_TOKENS
_CLAUDE_INPUT_TOKENS = support._CLAUDE_INPUT_TOKENS
_CLAUDE_MODEL = support._CLAUDE_MODEL
_CLAUDE_OUTPUT_TOKENS = support._CLAUDE_OUTPUT_TOKENS
_CODEX_MODEL = support._CODEX_MODEL
_DEVELOP_SKILL = support._DEVELOP_SKILL
_EVENT_KEY = support._EVENT_KEY
_FAKE_WT = support._FAKE_WT
_IGNORED_PROMPT = support._IGNORED_PROMPT
_INPUT_TOKENS_KEY = support._INPUT_TOKENS_KEY
_OUTPUT_TOKENS_KEY = support._OUTPUT_TOKENS_KEY
_REPORTED_COST_USD = support._REPORTED_COST_USD
_REVIEW_SKILL = support._REVIEW_SKILL
_RUN_AGENT_ATTR = support._RUN_AGENT_ATTR
_SKILL_KEY = support._SKILL_KEY
_SKILL_OUTPUT_TOKENS = support._SKILL_OUTPUT_TOKENS
_TRACK_SKILLS_ATTR = support._TRACK_SKILLS_ATTR
_TRAJECTORY_PATH_ATTR = support._TRAJECTORY_PATH_ATTR
_USAGE_HELPER_ISSUE_NUMBER = support._USAGE_HELPER_ISSUE_NUMBER
_USAGE_KEY = support._USAGE_KEY
_analytics_path = support._analytics_path
_analytics_records = support._analytics_records
_claude_stdout = support._claude_stdout
_claude_stdout_with_skills = support._claude_stdout_with_skills
_codex_stdout_no_model = support._codex_stdout_no_model


def _run_usage(
    *,
    stdout: str,
    backend: str = BACKEND_CLAUDE,
    track: bool = False,
    analytics_path: Optional[Path] = None,
    extra_args: tuple[str, ...] = (),
) -> tuple[FakeGitHubClient, AgentResult]:
    gh = FakeGitHubClient()
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(analytics, _TRAJECTORY_PATH_ATTR, None), \
            patch.object(analytics, _TRACK_SKILLS_ATTR, track), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-usage",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )
        tracked_result = workflow._run_agent_tracked(
            gh, _USAGE_HELPER_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=backend,
            prompt=_IGNORED_PROMPT,
            cwd=_FAKE_WT,
            agent_spec=backend,
            extra_args=extra_args,
            review_round=2,
            retry_count=1,
        )
    return gh, tracked_result


def _assert_usage_metrics(
    case: unittest.TestCase,
    metrics: usage.UsageMetrics,
) -> None:
    case.assertEqual(metrics.backend, BACKEND_CLAUDE)
    case.assertEqual(metrics.input_tokens, _CLAUDE_INPUT_TOKENS)
    case.assertEqual(metrics.output_tokens, _CLAUDE_OUTPUT_TOKENS)
    case.assertEqual(metrics.cache_read_tokens, 100)
    case.assertEqual(
        metrics.cache_write_tokens,
        _CLAUDE_CACHE_WRITE_TOKENS,
    )
    case.assertEqual(list(metrics.models), [_CLAUDE_MODEL])
    case.assertEqual(metrics.turns, 2)
    case.assertEqual(metrics.cost_source, "reported")
    case.assertAlmostEqual(metrics.cost_usd, _REPORTED_COST_USD)


class RunUsageSurfacedTest(unittest.TestCase):
    """Per-issue usage plumbing: `_run_agent_tracked` returns an `AgentResult`
    whose `usage` field carries the same `UsageMetrics` `record_agent_exit`
    parsed for the analytics record -- surfaced even when the sink is off,
    left `None` when the usage parse fails (fail-open), and never disturbing
    the analytics record or the `skill_triggered` audit events."""

    def test_agent_result_usage_defaults_to_none(self) -> None:
        # The new field is defaulted so every existing construction stays
        # valid without passing it; an untracked result carries no usage.
        agent_result = AgentResult(
            session_id="s", last_message="", exit_code=0,
            timed_out=False, stdout="", stderr="",
        )
        self.assertIsNone(agent_result.usage)

    def test_result_carries_usage_without_sink(self) -> None:
        # Sink OFF: the parsed metrics still reach the caller off `.usage`,
        # proving the plumbing is independent of the observability sink.
        gh, agent_result = _run_usage(
            stdout=_claude_stdout(total_cost_usd=_REPORTED_COST_USD),
            analytics_path=None,
        )
        self.assertIsInstance(agent_result.usage, usage.UsageMetrics)
        _assert_usage_metrics(self, agent_result.usage)
        # The lifecycle audit still fired even with the sink disabled.
        self.assertIn(
            EVENT_AGENT_EXIT, {event[_EVENT_KEY] for event in gh.recorded_events},
        )

    def test_usage_reflects_spec_fallback_model(self) -> None:
        # The surfaced metrics are the SAME object the record used, so the
        # codex spec-fallback model path (extra_args -> `_configured_model`
        # -> `fallback_model`) is visible on `.usage` too.
        _, agent_result = _run_usage(
            stdout=_codex_stdout_no_model(),
            backend=BACKEND_CODEX,
            extra_args=("-m", _CODEX_MODEL),
        )
        self.assertIsNotNone(agent_result.usage)
        self.assertEqual(list(agent_result.usage.models), [_CODEX_MODEL])
        self.assertEqual(agent_result.usage.cost_source, "estimated")

    def test_parse_failure_leaves_none_and_fails_open(self) -> None:
        path = _analytics_path(self, "usage-failopen-")
        with patch.object(
            analytics.usage,
            "parse_agent_usage",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs(analytics.log, level="ERROR"):
            github, agent_result = _run_usage(
                stdout=_claude_stdout(),
                analytics_path=path,
            )
        self.assertEqual(agent_result.session_id, "sess-usage")
        self.assertIsNone(agent_result.usage)
        self.assertEqual(_analytics_records(path), [])
        self.assertIn(
            EVENT_AGENT_EXIT,
            {
                event[_EVENT_KEY]
                for event in github.recorded_events
            },
        )

    def test_analytics_and_skill_events_unchanged(self) -> None:
        path = _analytics_path(self, "usage-unchanged-")
        github, agent_result = _run_usage(
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP_SKILL, _REVIEW_SKILL),
            ),
            track=True,
            analytics_path=path,
        )
        exit_record = _analytics_records(path)[0]
        self.assertEqual(exit_record[_EVENT_KEY], EVENT_AGENT_EXIT)
        self.assertEqual(exit_record[_INPUT_TOKENS_KEY], 1000)
        self.assertEqual(
            exit_record[_OUTPUT_TOKENS_KEY],
            _SKILL_OUTPUT_TOKENS,
        )
        self.assertNotIn(_USAGE_KEY, exit_record)
        self.assertEqual(agent_result.usage.input_tokens, 1000)
        self.assertEqual(
            agent_result.usage.output_tokens,
            _SKILL_OUTPUT_TOKENS,
        )
        skill_events = [
            event
            for event in github.recorded_events
            if event[_EVENT_KEY] == EVENT_SKILL_TRIGGERED
        ]
        self.assertEqual(
            [event[_SKILL_KEY] for event in skill_events],
            [_DEVELOP_SKILL, _REVIEW_SKILL],
        )
