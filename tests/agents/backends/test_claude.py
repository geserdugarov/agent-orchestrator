# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude backend command, resume, interruption, and last-message gating."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from orchestrator.agents import models as _models
from orchestrator.agents.backends import claude as _claude
from tests.agents import agent_test_support as _support
from tests.agents import agent_test_values as _agent_cases

_RESUME_SESSION_ID = "deadbeef-1234-1234-1234-1234deadbeef"


def _claude_argv(
    *,
    extra_args: tuple[str, ...] = (),
    resume_session_id: str | None = None,
) -> list[str]:
    return _claude.claude_command(
        _agent_cases._PROMPT,
        _models.AgentRunOptions(
            extra_args=extra_args,
            resume_session_id=resume_session_id,
        ),
    )


class ClaudeCommandTest(unittest.TestCase):
    """Argv shape, `extra_args` placement, stream-json flags, and resume."""

    def test_default_argv_pins_flag_order(self) -> None:
        # The fixed safety/output block and its ordering are load-bearing: the
        # stream-json output format plus partial-message streaming is how the
        # session parser and the final-message fallback read Claude's output.
        self.assertEqual(
            _claude_argv()[1:],
            [
                "-p",
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                _agent_cases._PROMPT,
            ],
        )

    def test_fresh_adds_args_before_safety(self) -> None:
        # `extra_args` lets a role-specific config inject backend-CLI flags
        # (e.g. `--model X --effort high`) right after the binary, ahead of the
        # orchestrator's own safety/output flags, which stay put.
        argv = _claude_argv(
            extra_args=(
                _agent_cases._CLAUDE_MODEL_FLAG,
                _agent_cases._CLAUDE_MODEL,
                "--effort",
                "high",
            ),
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
        self.assertEqual(argv[5], "-p")
        self.assertEqual(argv[-1], _agent_cases._PROMPT)

    def test_resume_keeps_args_and_appends_flag(self) -> None:
        argv = _claude_argv(
            extra_args=(_agent_cases._CLAUDE_MODEL_FLAG, _agent_cases._CLAUDE_MODEL),
            resume_session_id=_RESUME_SESSION_ID,
        )
        self.assertEqual(
            argv[1:3],
            [_agent_cases._CLAUDE_MODEL_FLAG, _agent_cases._CLAUDE_MODEL],
        )
        # `--resume <sid>` is appended after the safety flags and right before
        # the prompt, regardless of extra_args.
        self.assertEqual(
            argv[-3:],
            [_agent_cases._RESUME_FLAG, _RESUME_SESSION_ID, _agent_cases._PROMPT],
        )


class ClaudeInterruptedResultTest(unittest.TestCase):
    """A run cut short by the shutdown sweep threads `interrupted` through.

    The SIGTERM/SIGKILL classification lives in the process owner; the Claude
    runner must carry that flag onto its `AgentResult`, distinct from a normal
    completion and from the orchestrator's own `timed_out` path.
    """

    def test_signal_exit_threads_interrupted_through(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=-signal.SIGKILL),
        ):
            agent_result = _claude.run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)


class ClaudeLastMessageGatingTest(unittest.TestCase):
    """The assistant/message fallback is a forward-compat crutch for clean
    runs only. An interrupted or non-zero claude run with no terminal
    `result` event must expose an empty `last_message` rather than treating
    the last streamed chunk as the agent's considered final answer.
    """

    def test_interrupted_no_result_is_empty(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(
                stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT,
                returncode=-signal.SIGTERM,
            ),
        ):
            agent_result = _claude.run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "")

    def test_nonzero_no_result_is_empty(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT, returncode=1),
        ):
            agent_result = _claude.run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.exit_code, 1)
        self.assertEqual(agent_result.last_message, "")

    def test_interrupted_result_is_kept(self) -> None:
        # A run that emitted the terminal result before being killed still
        # surfaces that result -- the gate only suppresses the partial-chunk
        # fallback, never the documented final-message channel.
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(
                stdout=_agent_cases._CLAUDE_PARTIAL_THEN_RESULT,
                returncode=-signal.SIGKILL,
            ),
        ):
            agent_result = _claude.run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, _agent_cases._RESULT_BEFORE_KILL)

    def test_clean_run_still_uses_assistant_fallback(self) -> None:
        # The clean-completion path keeps the forward-compat fallback so a
        # schema drift that drops the result event does not silently blank the
        # final message on a successful run.
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT, returncode=0),
        ):
            agent_result = _claude.run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "partial work so far")


if __name__ == "__main__":
    unittest.main()
