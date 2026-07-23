# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configured-model fallbacks for tracked-agent analytics."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import analytics, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient

from tests import workflow_agent_analytics_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
ROLE_DEVELOPER = support.ROLE_DEVELOPER
_ANALYTICS_FILENAME = support._ANALYTICS_FILENAME
_ANALYTICS_PATH_ATTR = support._ANALYTICS_PATH_ATTR
_CLAUDE_FALLBACK_ISSUE_NUMBER = support._CLAUDE_FALLBACK_ISSUE_NUMBER
_CLAUDE_MODEL = support._CLAUDE_MODEL
_CODEX_CACHED_TOKENS = support._CODEX_CACHED_TOKENS
_CODEX_FALLBACK_ISSUE_NUMBER = support._CODEX_FALLBACK_ISSUE_NUMBER
_CODEX_INPUT_TOKENS = support._CODEX_INPUT_TOKENS
_CODEX_MODEL = support._CODEX_MODEL
_CODEX_OUTPUT_TOKENS = support._CODEX_OUTPUT_TOKENS
_COST_USD_KEY = support._COST_USD_KEY
_FAKE_WT = support._FAKE_WT
_IGNORED_PROMPT = support._IGNORED_PROMPT
_INPUT_TOKENS_KEY = support._INPUT_TOKENS_KEY
_OUTPUT_TOKENS_KEY = support._OUTPUT_TOKENS_KEY
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_RUN_AGENT_ATTR = support._RUN_AGENT_ATTR
_TRAJECTORY_PATH_ATTR = support._TRAJECTORY_PATH_ATTR
_analytics_path = support._analytics_path
_analytics_records = support._analytics_records
_claude_stdout = support._claude_stdout
_codex_stdout_no_model = support._codex_stdout_no_model


def _assert_codex_fallback_model(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record["backend"], BACKEND_CODEX)
    case.assertEqual(
        record["agent_spec"],
        f"codex -m {_CODEX_MODEL}",
    )
    case.assertEqual(record["models"], [_CODEX_MODEL])
    case.assertEqual(record["cost_source"], "estimated")
    case.assertIn(_COST_USD_KEY, record)
    case.assertGreater(record[_COST_USD_KEY], 0)


def _assert_codex_fallback_usage(
    case: unittest.TestCase,
    record: dict,
) -> None:
    case.assertEqual(record[_INPUT_TOKENS_KEY], _CODEX_INPUT_TOKENS)
    case.assertEqual(record["cached_tokens"], _CODEX_CACHED_TOKENS)
    case.assertEqual(record[_OUTPUT_TOKENS_KEY], _CODEX_OUTPUT_TOKENS)


class AgentAnalyticsModelFallbackTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Configured models fill only streams that omit their model."""

    def test_codex_no_model_uses_spec_fallback(self) -> None:
        path = _analytics_path(self, "analytics-codex-fallback-")
        with patch.object(
            analytics,
            _ANALYTICS_PATH_ATTR,
            path,
        ), patch.object(
            analytics,
            _TRAJECTORY_PATH_ATTR,
            None,
        ), patch.object(
            workflow,
            _RUN_AGENT_ATTR,
        ) as run_mock:
            run_mock.return_value = AgentResult(
                session_id="sess-codex",
                last_message="",
                exit_code=0,
                timed_out=False,
                stdout=_codex_stdout_no_model(),
                stderr="",
            )
            github = FakeGitHubClient()
            workflow._run_agent_tracked(
                github,
                _CODEX_FALLBACK_ISSUE_NUMBER,
                agent_role=ROLE_DEVELOPER,
                stage=LABEL_IMPLEMENTING,
                backend=BACKEND_CODEX,
                prompt=_IGNORED_PROMPT,
                cwd=_FAKE_WT,
                agent_spec=f"codex -m {_CODEX_MODEL}",
                extra_args=("-m", _CODEX_MODEL),
                retry_count=1,
            )

        records = _analytics_records(path)
        self.assertEqual(len(records), 1)
        record = records[0]
        _assert_codex_fallback_model(self, record)
        _assert_codex_fallback_usage(self, record)

    def test_claude_model_ignores_spec_fallback(self) -> None:
        path = _analytics_path(self, "analytics-claude-fallback-")
        with patch.object(
            analytics,
            _ANALYTICS_PATH_ATTR,
            path,
        ), patch.object(
            analytics,
            _TRAJECTORY_PATH_ATTR,
            None,
        ), patch.object(
            workflow,
            _RUN_AGENT_ATTR,
        ) as run_mock:
            run_mock.return_value = AgentResult(
                session_id="sess-claude",
                last_message="",
                exit_code=0,
                timed_out=False,
                stdout=_claude_stdout(model=_CLAUDE_MODEL),
                stderr="",
            )
            github = FakeGitHubClient()
            workflow._run_agent_tracked(
                github,
                _CLAUDE_FALLBACK_ISSUE_NUMBER,
                agent_role=ROLE_DEVELOPER,
                stage=LABEL_IMPLEMENTING,
                backend=BACKEND_CLAUDE,
                prompt=_IGNORED_PROMPT,
                cwd=_FAKE_WT,
                agent_spec="claude --model claude-opus-4-7",
                extra_args=("--model", "claude-opus-4-7"),
                retry_count=1,
            )

        records = _analytics_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["models"], [_CLAUDE_MODEL])
