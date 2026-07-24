# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Backend command construction and argv-shape tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class RunCodexCwdTest(unittest.TestCase):
    def test_dash_C_gets_full_path_for_relative_cwd(self) -> None:
        # codex applies `-C` AFTER it has already chdir'd into the subprocess
        # cwd, so a relative path resolves twice and codex hits "No such file
        # or directory (os error 2)". Pinning this guarantees the path passed
        # to `-C` is absolute even when WORKTREES_DIR (and the worktree path
        # derived from it) is relative.
        rel_cwd = Path("../wt-orchestrator/foo/issue-1")
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(),
        ) as run_mock:
            _agents._run_codex(_agent_cases._PROMPT, rel_cwd)
            argv = list(run_mock.call_args.args[0])
        c_value = argv[argv.index("-C") + 1]
        self.assertTrue(
            Path(c_value).is_absolute(),
            f"-C path should be absolute, got {c_value!r}",
        )
        self.assertEqual(Path(c_value), rel_cwd.resolve())


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
    (e.g. `-m gpt-5.5` for codex, `--model X --effort high` for claude)
    into the spawned argv on both fresh and resumed runs while keeping the
    safety/output flags and prompt where they already are.
    """

    def test_codex_fresh_adds_args_before_exec(self) -> None:
        # Codex global options (`-m`, `-c`) must appear BEFORE the `exec`
        # subcommand; the parser rejects them after the subcommand. The
        # safety/output flags and prompt must remain on the argv tail.
        argv = self._argv_for(
            _agent_cases._CODEX,
            extra_args=(
                _agent_cases._MODEL_FLAG,
                _agent_cases._CODEX_MODEL,
                _agent_cases._CONFIG_FLAG,
                'model_reasoning_effort="xhigh"',
            ),
        )
        self.assertEqual(
            argv[1:5],
            [
                _agent_cases._MODEL_FLAG,
                _agent_cases._CODEX_MODEL,
                _agent_cases._CONFIG_FLAG,
                'model_reasoning_effort="xhigh"',
            ],
        )
        self.assertEqual(argv[5], _agent_cases._CODEX_EXEC)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], _agent_cases._PROMPT)

    def test_codex_resume_adds_args_before_exec(self) -> None:
        sid = "11111111-2222-3333-4444-555555555555"
        argv = self._argv_for(
            _agent_cases._CODEX,
            extra_args=(_agent_cases._MODEL_FLAG, _agent_cases._CODEX_MODEL),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], [_agent_cases._MODEL_FLAG, _agent_cases._CODEX_MODEL])
        self.assertEqual(argv[3:5], [_agent_cases._CODEX_EXEC, "resume"])
        # Resume session id and prompt are still the last two tokens; the
        # extra args must NOT have displaced them.
        self.assertEqual(argv[-2:], [sid, _agent_cases._PROMPT])

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
        # the legacy argv with no inserted tokens. Sanity-checks both
        # backends so a future refactor that changes argv shape under
        # default callers fails this test loudly.
        codex_argv = self._argv_for(_agent_cases._CODEX, extra_args=())
        self.assertEqual(codex_argv[1], _agent_cases._CODEX_EXEC)
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
