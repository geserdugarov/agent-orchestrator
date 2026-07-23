# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused agent runtime tests."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class RunSubprocessRegistrationTest(unittest.TestCase):
    """`_run_subprocess` must register its child for the lifetime of the run
    so the shutdown sweep can reach it, and clear it afterward so the registry
    does not leak completed processes.
    """

    def test_registers_during_run_and_clears_after(self) -> None:
        proc = _support.completed(stdout="{}", returncode=0)
        registration_probe = _support.RegistrationProbe(proc)
        proc.communicate.side_effect = registration_probe
        with patch(_agent_cases._POPEN_TARGET, return_value=proc):
            _agents._run_subprocess([_agent_cases._AGENT_COMMAND], _agent_cases._CWD, {}, 10)

        self.assertTrue(registration_probe.seen, "child not registered during the run")
        with _agents._running_procs_lock:
            self.assertNotIn(proc, _agents._running_procs)


class CommunicateBoundedTest(unittest.TestCase):
    """`_communicate_bounded` is the shared drain primitive both the agent
    runner and the verify runner call. Its contract: return the captured
    streams (coercing an absent stream to ``""``) on completion, and ``None``
    when the drain itself blocks past the cap so the caller can escalate.
    """

    def test_returns_streams_coercing_absent_to_empty(self) -> None:
        proc = MagicMock()
        proc.communicate.return_value = (None, None)
        self.assertEqual(_agents._communicate_bounded(proc, 5), ("", ""))

    def test_returns_none_on_timeout(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=_agent_cases._AGENT_COMMAND,
            timeout=5,
        )
        self.assertIsNone(_agents._communicate_bounded(proc, 5))
