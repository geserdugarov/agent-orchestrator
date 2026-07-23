# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process doubles and cleanup helpers shared by agent tests."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from unittest.mock import MagicMock

from orchestrator import agents as _agents
from tests import agent_test_values as _agent_cases


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    process = MagicMock()
    process.communicate.return_value = (stdout, stderr)
    process.returncode = returncode
    process.pid = _agent_cases._MOCK_PID
    return process


def killpg_group_empty(_pid: int, sent_signal: int) -> None:
    if sent_signal == 0:
        raise ProcessLookupError


def killpg_group_alive(_pid: int, _sent_signal: int) -> None:
    """Keep the process-group liveness probe successful."""


@contextlib.contextmanager
def registered_procs(*processes: object):
    with contextlib.ExitStack() as cleanup:
        for process in processes:
            _agents._register_proc(process)
            cleanup.callback(_agents._unregister_proc, process)
        yield


def stop_process_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=_agent_cases._PROCESS_WAIT_SECONDS)


class RegistrationProbe:
    def __init__(self, process: object) -> None:
        self.process = process
        self.seen = False

    def __call__(self, *unused_args, **unused_kwargs) -> tuple[str, str]:
        with _agents._running_procs_lock:
            self.seen = self.process in _agents._running_procs
        return "{}", ""
