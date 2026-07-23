# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree plumbing serialization: the per-`target_root` lock that keeps
`_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`
from racing on `.git/config.lock` when `tick()` fans non-family-aware
stages out across worker threads. Covers both the deterministic blocking-
fake unit tests and a real-git integration smoke test against a real bare
remote."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator import (
    config,
    worktrees,
)

GIT_COMMAND = "git"
BASE_BRANCH = "main"
ORIGIN_REMOTE = "origin"
PROBE_DELAY_SECONDS = 0.02
THREAD_TIMEOUT_SECONDS = 10.0
BARRIER_TIMEOUT_SECONDS = 5.0
REAL_GIT_TIMEOUT_SECONDS = 30.0


def _git_result() -> MagicMock:
    return MagicMock(returncode=0, stdout="", stderr="")


def _has_no_new_commits(*_args, **_kwargs) -> bool:
    return False


def _local_fetch(spec, branch):
    return subprocess.run(
        [GIT_COMMAND, "fetch", "--quiet", spec.remote_name, branch],
        cwd=str(spec.target_root),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _run_git(
    *args: str,
    cwd: Path,
    env_extra: dict | None = None,
) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        env.update(env_extra)
    git_result = subprocess.run(
        [GIT_COMMAND, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return git_result.stdout


def _start_and_join(threads: list[threading.Thread], *, timeout: float) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)


class _ConcurrencyProbe:
    def __init__(
        self,
        *,
        delay: float = 0,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.maximum_in_flight = 0
        self.order: list[str] = []
        self._in_flight = 0
        self._delay = delay
        self._barrier = barrier
        self._lock = threading.Lock()

    def record(self, label: str) -> MagicMock:
        with self._lock:
            self._in_flight += 1
            self.maximum_in_flight = max(
                self.maximum_in_flight,
                self._in_flight,
            )
            self.order.append(label)
        try:
            self._hold()
        except BaseException:
            self._leave()
            raise
        else:
            self._leave()
        return _git_result()

    def git(self, *args, cwd) -> MagicMock:
        return self.record(f"{args[0]}({threading.get_ident()})")

    def fetch(self, _spec, _branch) -> MagicMock:
        return self.record(f"fetch({threading.get_ident()})")

    def subprocess_run(self, args, **_kwargs) -> MagicMock:
        if "fetch" in args and "--quiet" in args:
            return self.record("fetch")
        return _git_result()

    def _hold(self) -> None:
        if self._barrier is not None:
            self._barrier.wait()
        if self._delay:
            time.sleep(self._delay)

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1


class _EnsureRecorder:
    def __init__(self, spec: config.RepoSpec) -> None:
        self.outcomes: list[tuple[int, Path | None, BaseException | None]] = []
        self._spec = spec
        self._lock = threading.Lock()

    def __call__(self, issue_number: int) -> None:
        try:
            outcome = (
                issue_number,
                worktrees._ensure_worktree(self._spec, issue_number),
                None,
            )
        except BaseException as error:  # noqa: BLE001 - asserted by the test
            outcome = (issue_number, None, error)
        with self._lock:
            self.outcomes.append(outcome)
