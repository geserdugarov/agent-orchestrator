# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude backend command construction and argv-shape tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class RunClaudeResumeTest(unittest.TestCase):
    def test_resume_passes_resume_session_id_arg(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(),
        ) as run_mock:
            _agents._run_claude("followup", _agent_cases._CWD, resume_session_id=sid)
            argv = list(run_mock.call_args.args[0])
        self.assertIn(_agent_cases._RESUME_FLAG, argv)
        self.assertEqual(argv[argv.index(_agent_cases._RESUME_FLAG) + 1], sid)


class RunAgentExtraArgsTest(unittest.TestCase):
    """`extra_args` lets a role-specific config inject backend-CLI flags
    (e.g. `--model X --effort high` for claude) into the spawned argv on both
    fresh and resumed runs while keeping the safety/output flags and prompt
    where they already are.
    """

    def test_claude_fresh_adds_args_before_safety(self) -> None:
        argv = self._argv_for(
            _agent_cases._CLAUDE,
            extra_args=(_agent_cases._CLAUDE_MODEL_FLAG, _agent_cases._CLAUDE_MODEL, "--effort", "high"),
        )
        self.assertEqual(
            argv[1:5],
            [
                _agent_cases._CLAUDE_MODEL_FLAG,
                _agent_cases._CLAUDE_MODEL,
                "--effort",
                "high",
            ],
        )
        # Safety + output flags survive immediately after the extra args.
        self.assertEqual(argv[5], "-p")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[-1], _agent_cases._PROMPT)

    def test_claude_resume_keeps_args_and_flag(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        argv = self._argv_for(
            _agent_cases._CLAUDE,
            extra_args=(_agent_cases._CLAUDE_MODEL_FLAG, _agent_cases._CLAUDE_MODEL),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], [_agent_cases._CLAUDE_MODEL_FLAG, _agent_cases._CLAUDE_MODEL])
        # `--resume <sid>` is appended after the safety flags and right
        # before the prompt, regardless of extra_args.
        self.assertIn(_agent_cases._RESUME_FLAG, argv)
        self.assertEqual(argv[argv.index(_agent_cases._RESUME_FLAG) + 1], sid)
        self.assertEqual(argv[-1], _agent_cases._PROMPT)

    def test_empty_default_keeps_argv_unchanged(self) -> None:
        # Backward compat: callers that don't pass `extra_args` still get
        # the legacy argv with no inserted tokens, so a future refactor that
        # changes argv shape under default callers fails this test loudly.
        claude_argv = self._argv_for(_agent_cases._CLAUDE, extra_args=())
        self.assertEqual(claude_argv[1], "-p")

    def _argv_for(
        self,
        backend: str,
        *,
        extra_args: tuple[str, ...],
        resume_session_id=None,
    ) -> list[str]:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(),
        ) as run_mock:
            _agents.run_agent(
                backend,
                _agent_cases._PROMPT,
                _agent_cases._CWD,
                resume_session_id=resume_session_id,
                extra_args=extra_args,
            )
            return list(run_mock.call_args.args[0])
