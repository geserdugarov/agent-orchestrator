# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused agent runtime tests."""

from __future__ import annotations

import json
import os
import signal
import sys
import unittest
from unittest.mock import patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class InterruptedSubprocessClassificationTest(unittest.TestCase):
    """A run cut short by SIGTERM/SIGKILL -- the shape the orchestrator's
    shutdown sweep (`terminate_all_running`) produces when it kills an
    in-flight agent group -- must surface as `interrupted=True`, distinct from
    a normal completion and from the orchestrator's own `timed_out` path.
    """

    def test_signal_exit_marked_interrupted(self) -> None:
        # Both shutdown-sweep signals produce a completed-but-interrupted run:
        # negative returncode, `interrupted=True`, and `timed_out=False`.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                *_, exit_code, timed_out, interrupted = self._kill_self(sig)
                self.assertEqual(exit_code, -sig)
                self.assertFalse(timed_out)
                self.assertTrue(interrupted)

    def test_clean_exit_not_interrupted(self) -> None:
        # A normal non-zero failure (exit 3) is a completed run, NOT an
        # interruption -- the two must stay distinguishable downstream.
        cmd = [sys.executable, _agent_cases._PYTHON_COMMAND_FLAG, "import sys; sys.exit(3)"]
        *_, exit_code, timed_out, interrupted = _agents._run_subprocess(
            cmd,
            _agent_cases._REAL_CWD,
            dict(os.environ),
            _agent_cases._SUBPROCESS_TIMEOUT_SECONDS,
        )
        self.assertEqual(exit_code, 3)
        self.assertFalse(timed_out)
        self.assertFalse(interrupted)

    def test_own_timeout_is_timed_out(self) -> None:
        # A child that outlives our own `timeout` drives the timeout branch:
        # `_terminate_process_group` reaps the group and the run is classified
        # `timed_out=True`, `interrupted=False`, exit_code=-1 -- distinct from
        # the shutdown-sweep interruption above even though both signal the
        # group. Real child + 1s timeout so the whole flatten path is exercised.
        cmd = [sys.executable, _agent_cases._PYTHON_COMMAND_FLAG, "import time; time.sleep(30)"]
        *_, exit_code, timed_out, interrupted = _agents._run_subprocess(
            cmd, _agent_cases._REAL_CWD, dict(os.environ), 1
        )
        self.assertEqual(exit_code, -1)
        self.assertTrue(timed_out)
        self.assertFalse(interrupted)

    def _kill_self(self, sig: signal.Signals) -> tuple[str, str, int, bool, bool]:
        # Drive a REAL child that signals itself, so the negative returncode is
        # produced by the kernel + Popen exactly as it is when the shutdown
        # sweep SIGTERMs/SIGKILLs the group, not synthesized by a mock.
        cmd = [
            sys.executable,
            _agent_cases._PYTHON_COMMAND_FLAG,
            f"import os, signal; os.kill(os.getpid(), {int(sig)})",
        ]
        return _agents._run_subprocess(
            cmd,
            _agent_cases._REAL_CWD,
            dict(os.environ),
            _agent_cases._SUBPROCESS_TIMEOUT_SECONDS,
        )


class InterruptedAgentResultTest(unittest.TestCase):
    """A run cut short by SIGTERM/SIGKILL -- the shape the orchestrator's
    shutdown sweep (`terminate_all_running`) produces when it kills an
    in-flight agent group -- must surface as `interrupted=True`, distinct from
    a normal completion and from the orchestrator's own `timed_out` path.
    """

    def test_run_codex_threads_interrupted_through(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=-signal.SIGTERM),
        ):
            agent_result = _agents._run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)
        self.assertEqual(agent_result.exit_code, -signal.SIGTERM)

    def test_run_claude_threads_interrupted_through(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=-signal.SIGKILL),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)

    def test_clean_run_reports_not_interrupted(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=0),
        ):
            agent_result = _agents.run_agent(_agent_cases._CODEX, _agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)

    def test_agent_result_interrupted_defaults_false(self) -> None:
        # Backwards-compat: existing positional/keyword constructions that omit
        # the new field still build and read `interrupted` as False.
        agent_result = _agents.AgentResult(
            session_id=None,
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
        )
        self.assertFalse(agent_result.interrupted)


class ClaudeLastMessageGatingTest(unittest.TestCase):
    """The assistant/message fallback is a forward-compat crutch for clean
    runs only. An interrupted or non-zero claude run with no terminal
    `result` event must expose an empty `last_message` rather than treating
    the last streamed chunk as the agent's considered final answer.
    """

    def test_fallback_gated_off_directly(self) -> None:
        # With the fallback disabled, a transcript carrying only assistant
        # chunks yields ""; a terminal result event is still honored.
        self.assertEqual(
            _agents._claude_last_message(
                _agent_cases._PARTIAL_CLAUDE_OUTPUT,
                allow_assistant_fallback=False,
            ),
            "",
        )
        result_frame = json.dumps(
            {_agent_cases._TYPE_FIELD: _agent_cases._RESULT_FIELD, _agent_cases._RESULT_FIELD: "final"}
        )
        with_result = f"{_agent_cases._PARTIAL_CLAUDE_OUTPUT}\n{result_frame}"
        self.assertEqual(
            _agents._claude_last_message(with_result, allow_assistant_fallback=False),
            "final",
        )

    def test_interrupted_no_result_is_empty(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(
                stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT,
                returncode=-signal.SIGTERM,
            ),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "")

    def test_nonzero_no_result_is_empty(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT, returncode=1),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.exit_code, 1)
        self.assertEqual(agent_result.last_message, "")

    def test_interrupted_result_is_kept(self) -> None:
        # A run that emitted the terminal result before being killed still
        # surfaces that result -- the gate only suppresses the partial-chunk
        # fallback, never the documented final-message channel.
        result_frame = json.dumps(
            {_agent_cases._TYPE_FIELD: _agent_cases._RESULT_FIELD, _agent_cases._RESULT_FIELD: "done before kill"},
        )
        out = f"{_agent_cases._PARTIAL_CLAUDE_OUTPUT}\n{result_frame}"
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout=out, returncode=-signal.SIGKILL),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "done before kill")

    def test_clean_run_still_uses_assistant_fallback(self) -> None:
        # The clean-completion path keeps the forward-compat fallback so a
        # schema drift that drops the result event does not silently blank the
        # final message on a successful run.
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout=_agent_cases._PARTIAL_CLAUDE_OUTPUT, returncode=0),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "partial work so far")
