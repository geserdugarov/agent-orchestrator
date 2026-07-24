# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared process registry and hardened subprocess-group lifecycle.

Agent runs and the verify runner both spawn children into their own process
group (``start_new_session=True``) and register the group leader here so the
shutdown sweep can reach an in-flight run. Process creation lives in this owner
so the historical ``orchestrator.agents.processes.subprocess.Popen`` patch
point and the shared shutdown registry keep their exact behavior; the
``orchestrator.agents`` facade re-exports only ``terminate_all_running``.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.agents import models as _agent_models

_running_procs: set[subprocess.Popen] = set()
_running_procs_lock = threading.Lock()

_INTERRUPTED_RETURNCODES = frozenset((-signal.SIGTERM, -signal.SIGKILL))


def register_proc(proc: subprocess.Popen) -> None:
    """Register a live process-group leader for shutdown cleanup."""
    with _running_procs_lock:
        _running_procs.add(proc)


def unregister_proc(proc: subprocess.Popen) -> None:
    """Remove a completed process-group leader from the registry."""
    with _running_procs_lock:
        _running_procs.discard(proc)


@contextmanager
def registered(proc: subprocess.Popen) -> Iterator[subprocess.Popen]:
    """Keep a process reachable by the shutdown sweep for one run."""
    register_proc(proc)
    try:
        yield proc
    finally:
        unregister_proc(proc)


def communicate_bounded(
    proc: subprocess.Popen,
    timeout: float,
) -> Optional[tuple[str, str]]:
    """Communicate within a wall-clock cap, returning ``None`` on timeout."""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return stdout or "", stderr or ""


def process_group_alive(process_group_id: int) -> bool:
    """Probe whether a process group still contains a live member."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def sigkill_unless_group_gone(
    proc: subprocess.Popen,
    timeout: float,
) -> None:
    """Wait for the leader, then SIGKILL any surviving process group."""
    leader_exited = True
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        leader_exited = False
    if leader_exited and not process_group_alive(proc.pid):
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def terminate_all_running(grace: float = 5.0) -> int:
    """SIGTERM every registered group, then SIGKILL deadline stragglers."""
    with _running_procs_lock:
        running_procs = list(_running_procs)
    if not running_procs:
        return 0
    for proc in running_procs:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    for proc in running_procs:
        remaining = max(0, deadline - time.monotonic())
        sigkill_unless_group_gone(proc, remaining)
    return len(running_procs)


def run_subprocess(
    command: list[str],
    cwd: Path,
    environ: dict[str, str],
    timeout: int,
) -> _agent_models.SubprocessResult:
    """Run one agent in a registered, independently killable process group."""
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with registered(proc):
        drained = communicate_bounded(proc, timeout)
        if drained is None:
            terminate_process_group(proc)
            drained = communicate_bounded(proc, 10)
            stdout, stderr = ("", "") if drained is None else drained
            return _agent_models.SubprocessResult(stdout, stderr, -1, True, False)
        stdout, stderr = drained
        interrupted = proc.returncode in _INTERRUPTED_RETURNCODES
        return _agent_models.SubprocessResult(
            stdout,
            stderr,
            proc.returncode,
            False,
            interrupted,
        )


def terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM one process group, then SIGKILL it if anything survives."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    sigkill_unless_group_gone(proc, timeout=5)
