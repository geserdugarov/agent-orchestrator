# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Registry shared by agent and verification subprocess groups."""
from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from typing import Iterator

_running_procs: set[subprocess.Popen] = set()
_running_procs_lock = threading.Lock()


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
