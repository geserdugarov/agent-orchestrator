# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process registry and subprocess-group lifecycle owner tests."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import agents as _agents
from orchestrator.agents import processes as _processes
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class RunSubprocessRegistrationTest(unittest.TestCase):
    """`run_subprocess` must register its child for the lifetime of the run
    so the shutdown sweep can reach it, and clear it afterward so the registry
    does not leak completed processes.
    """

    def test_registers_during_run_and_clears_after(self) -> None:
        proc = _support.completed(stdout="{}", returncode=0)
        registration_probe = _support.RegistrationProbe(proc)
        proc.communicate.side_effect = registration_probe
        with patch(_agent_cases._POPEN_TARGET, return_value=proc):
            _processes.run_subprocess([_agent_cases._AGENT_COMMAND], _agent_cases._CWD, {}, 10)

        self.assertTrue(registration_probe.seen, "child not registered during the run")
        with _processes._running_procs_lock:
            self.assertNotIn(proc, _processes._running_procs)


class CommunicateBoundedTest(unittest.TestCase):
    """`communicate_bounded` is the shared drain primitive both the agent
    runner and the verify runner call. Its contract: return the captured
    streams (coercing an absent stream to ``""``) on completion, and ``None``
    when the drain itself blocks past the cap so the caller can escalate.
    """

    def test_returns_streams_coercing_absent_to_empty(self) -> None:
        proc = MagicMock()
        proc.communicate.return_value = (None, None)
        self.assertEqual(_processes.communicate_bounded(proc, 5), ("", ""))

    def test_returns_none_on_timeout(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=_agent_cases._AGENT_COMMAND,
            timeout=5,
        )
        self.assertIsNone(_processes.communicate_bounded(proc, 5))


class TerminateAllRunningTest(unittest.TestCase):
    """`terminate_all_running` is the shutdown hook that kills in-flight agent
    process groups so a restart does not hang for up to `AGENT_TIMEOUT`. It
    must SIGTERM every registered group, SIGKILL anything still alive at the
    shared grace deadline, and be a clean no-op when nothing is in flight.
    """

    def test_no_procs_is_noop(self) -> None:
        # Registry empty between tests (every spawn unregisters in a finally),
        # so this exercises the early return with no signals sent.
        with patch.object(_processes.os, _agent_cases._KILLPG) as killpg:
            self.assertEqual(_processes.terminate_all_running(), 0)
            killpg.assert_not_called()

    def test_no_sigkill_after_all_groups_exit(self) -> None:
        # Both leaders exit on SIGTERM and the signal-0 group probe reports the
        # group empty, so no SIGKILL is sent -- the clean-shutdown path.
        proc1, proc2 = MagicMock(), MagicMock()
        proc1.pid = 111
        proc2.pid = 222
        proc1.wait.return_value = 0
        proc2.wait.return_value = 0
        with _support.registered_procs(proc1, proc2):
            with patch.object(
                _processes.os,
                _agent_cases._KILLPG,
                side_effect=_support.killpg_group_empty,
            ) as signal_mock:
                terminated_count = _processes.terminate_all_running(grace=0.5)
                sent = {call.args for call in signal_mock.call_args_list}
        self.assertEqual(terminated_count, 2)
        self.assertIn((111, signal.SIGTERM), sent)
        self.assertIn((222, signal.SIGTERM), sent)
        self.assertNotIn((111, signal.SIGKILL), sent)
        self.assertNotIn((222, signal.SIGKILL), sent)

    def test_sigkill_if_child_outlives_leader(self) -> None:
        # Regression: the leader exits on SIGTERM but a descendant in the same
        # group ignored it. `proc.wait()` returns, yet the signal-0 probe shows
        # the group still alive, so the group must be SIGKILLed -- otherwise the
        # grandchild keeps mutating the worktree after the orchestrator exits.
        proc = MagicMock()
        proc.pid = 555
        proc.wait.return_value = 0  # leader exits promptly on SIGTERM
        with _support.registered_procs(proc):
            with patch.object(
                _processes.os,
                _agent_cases._KILLPG,
                side_effect=_support.killpg_group_alive,
            ) as signal_mock:
                _processes.terminate_all_running(grace=_agent_cases._TERMINATION_GRACE_SECONDS)
                sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((555, signal.SIGTERM), sent)
        self.assertIn((555, 0), sent)  # group liveness probed after leader exit
        self.assertIn((555, signal.SIGKILL), sent)

    def test_sigkills_straggler_past_deadline(self) -> None:
        # A group that never exits on SIGTERM must be SIGKILLed once the
        # shared grace deadline elapses.
        proc = MagicMock()
        proc.pid = 333
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd=_agent_cases._AGENT_COMMAND,
            timeout=_agent_cases._TERMINATION_GRACE_SECONDS,
        )
        with _support.registered_procs(proc):
            with patch.object(_processes.os, _agent_cases._KILLPG) as killpg:
                _processes.terminate_all_running(grace=_agent_cases._TERMINATION_GRACE_SECONDS)
                calls = [call.args for call in killpg.call_args_list]
        self.assertIn((333, signal.SIGTERM), calls)
        self.assertIn((333, signal.SIGKILL), calls)

    def test_missing_group_is_swallowed(self) -> None:
        # The leader can exit between the snapshot and the killpg; the
        # ProcessLookupError race must not propagate.
        proc = MagicMock()
        proc.pid = 444
        proc.wait.return_value = 0
        with _support.registered_procs(proc):
            with patch.object(
                _processes.os,
                _agent_cases._KILLPG,
                side_effect=ProcessLookupError,
            ):
                self.assertEqual(
                    _processes.terminate_all_running(
                        grace=_agent_cases._TERMINATION_GRACE_SECONDS,
                    ),
                    1,
                )

    def test_process_group_alive_real_process(self) -> None:
        # The mock tests can't exercise the actual `killpg(_, 0)` probe the
        # SIGKILL decision now relies on, so drive a real process group:
        # alive while the leader runs, empty once it is killed and reaped.
        proc = subprocess.Popen(
            [sys.executable, _agent_cases._PYTHON_COMMAND_FLAG, "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(_support.stop_process_group, proc)
            self.assertTrue(_processes.process_group_alive(proc.pid))
        self.assertFalse(_processes.process_group_alive(proc.pid))


class TerminateProcessGroupTest(unittest.TestCase):
    """`terminate_process_group` is the per-timeout cleanup. It must mirror
    `terminate_all_running`'s safety model: after the leader exits it probes
    the group with `killpg(_, 0)` and SIGKILLs any surviving descendant, so a
    build grandchild the agent forked cannot keep mutating the worktree after
    the timeout has already been recorded.
    """

    def test_sigkill_if_child_outlives_leader(self) -> None:
        # The leader exits on SIGTERM but a descendant in the same group
        # ignored it. `proc.wait()` returns, yet the signal-0 probe shows the
        # group still alive, so the group must be SIGKILLed.
        proc = MagicMock()
        proc.pid = 777
        proc.wait.return_value = 0  # leader exits promptly on SIGTERM

        with patch.object(
            _processes.os,
            _agent_cases._KILLPG,
            side_effect=_support.killpg_group_alive,
        ) as signal_mock:
            _processes.terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((777, signal.SIGTERM), sent)
        self.assertIn((777, 0), sent)  # group liveness probed after leader exit
        self.assertIn((777, signal.SIGKILL), sent)

    def test_no_sigkill_when_group_fully_exited(self) -> None:
        # Leader exits and the signal-0 probe reports the group empty, so no
        # SIGKILL is sent -- the clean path.
        proc = MagicMock()
        proc.pid = 778
        proc.wait.return_value = 0

        with patch.object(
            _processes.os,
            _agent_cases._KILLPG,
            side_effect=_support.killpg_group_empty,
        ) as signal_mock:
            _processes.terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((778, signal.SIGTERM), sent)
        self.assertIn((778, 0), sent)
        self.assertNotIn((778, signal.SIGKILL), sent)

    def test_sigkills_straggler_past_deadline(self) -> None:
        # The leader never exits on SIGTERM; once the grace `wait` times out
        # the group is SIGKILLed without a probe (a live leader means a live
        # group).
        proc = MagicMock()
        proc.pid = 779
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd=_agent_cases._AGENT_COMMAND,
            timeout=5,
        )
        with patch.object(_processes.os, _agent_cases._KILLPG) as killpg:
            _processes.terminate_process_group(proc)
            calls = [call.args for call in killpg.call_args_list]
        self.assertIn((779, signal.SIGTERM), calls)
        self.assertIn((779, signal.SIGKILL), calls)
        self.assertNotIn((779, 0), calls)  # no probe when the leader is alive

    def test_first_sigterm_lookup_needs_no_kill(self) -> None:
        # The group already exited between the timeout firing and the killpg;
        # the ProcessLookupError race short-circuits before any wait/SIGKILL.
        proc = MagicMock()
        proc.pid = 780
        with patch.object(
            _processes.os,
            _agent_cases._KILLPG,
            side_effect=ProcessLookupError,
        ) as signal_mock:
            _processes.terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertEqual(
            sent,
            [(780, signal.SIGTERM)],
        )
        proc.wait.assert_not_called()


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
        *_, exit_code, timed_out, interrupted = _processes.run_subprocess(
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
        # `terminate_process_group` reaps the group and the run is classified
        # `timed_out=True`, `interrupted=False`, exit_code=-1 -- distinct from
        # the shutdown-sweep interruption above even though both signal the
        # group. Real child + 1s timeout so the whole flatten path is exercised.
        cmd = [sys.executable, _agent_cases._PYTHON_COMMAND_FLAG, "import time; time.sleep(30)"]
        *_, exit_code, timed_out, interrupted = _processes.run_subprocess(
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
        return _processes.run_subprocess(
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
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(
                stdout=_agent_cases._CLAUDE_PARTIAL_THEN_RESULT,
                returncode=-signal.SIGKILL,
            ),
        ):
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
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
            agent_result = _agents._run_claude(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "partial work so far")
