# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused agent runtime tests."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class TerminateAllRunningTest(unittest.TestCase):
    """`terminate_all_running` is the shutdown hook that kills in-flight agent
    process groups so a restart does not hang for up to `AGENT_TIMEOUT`. It
    must SIGTERM every registered group, SIGKILL anything still alive at the
    shared grace deadline, and be a clean no-op when nothing is in flight.
    """

    def test_no_procs_is_noop(self) -> None:
        # Registry empty between tests (every spawn unregisters in a finally),
        # so this exercises the early return with no signals sent.
        with patch.object(_agents.os, _agent_cases._KILLPG) as killpg:
            self.assertEqual(_agents.terminate_all_running(), 0)
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
                _agents.os,
                _agent_cases._KILLPG,
                side_effect=_support.killpg_group_empty,
            ) as signal_mock:
                terminated_count = _agents.terminate_all_running(grace=0.5)
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
                _agents.os,
                _agent_cases._KILLPG,
                side_effect=_support.killpg_group_alive,
            ) as signal_mock:
                _agents.terminate_all_running(grace=_agent_cases._TERMINATION_GRACE_SECONDS)
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
            with patch.object(_agents.os, _agent_cases._KILLPG) as killpg:
                _agents.terminate_all_running(grace=_agent_cases._TERMINATION_GRACE_SECONDS)
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
                _agents.os,
                _agent_cases._KILLPG,
                side_effect=ProcessLookupError,
            ):
                self.assertEqual(
                    _agents.terminate_all_running(
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
            self.assertTrue(_agents._process_group_alive(proc.pid))
        self.assertFalse(_agents._process_group_alive(proc.pid))


class TerminateProcessGroupTest(unittest.TestCase):
    """`_terminate_process_group` is the per-timeout cleanup. It must mirror
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
            _agents.os,
            _agent_cases._KILLPG,
            side_effect=_support.killpg_group_alive,
        ) as signal_mock:
            _agents._terminate_process_group(proc)
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
            _agents.os,
            _agent_cases._KILLPG,
            side_effect=_support.killpg_group_empty,
        ) as signal_mock:
            _agents._terminate_process_group(proc)
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
        with patch.object(_agents.os, _agent_cases._KILLPG) as killpg:
            _agents._terminate_process_group(proc)
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
            _agents.os,
            _agent_cases._KILLPG,
            side_effect=ProcessLookupError,
        ) as signal_mock:
            _agents._terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertEqual(
            sent,
            [(780, signal.SIGTERM)],
        )
        proc.wait.assert_not_called()
