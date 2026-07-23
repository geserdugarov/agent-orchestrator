# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree plumbing serialization: the per-`target_root` lock that keeps
`_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`
from racing on `.git/config.lock` when `tick()` fans non-family-aware
stages out across worker threads. Covers both the deterministic blocking-
fake unit tests and a real-git integration smoke test against a real bare
remote."""

from __future__ import annotations

import unittest

from tests.worktree_real_git_support import (
    _RealGitWorktreeRepo,
    _assert_ensure_outcomes,
    _assert_worktrees_exist,
    _run_ensure_workers,
)

from tests.worktree_serialization_support import (
    _EnsureRecorder,
)

GIT_COMMAND = "git"
BASE_BRANCH = "main"
ORIGIN_REMOTE = "origin"
PROBE_DELAY_SECONDS = 0.02
THREAD_TIMEOUT_SECONDS = 10.0
BARRIER_TIMEOUT_SECONDS = 5.0
REAL_GIT_TIMEOUT_SECONDS = 30.0
ISSUE_NUMBERS = tuple(range(1, 7))


class EnsureWorktreeRealGitConcurrencyTest(unittest.TestCase):
    """Integration smoke test for the per-target_root lock: drive multiple
    real `_ensure_worktree` calls against a real bare remote concurrently.

    Without the lock, even at 2 workers `git worktree add` would
    intermittently report `error: could not lock config file .git/config:
    File exists` (the reviewer's reproducer). With the lock, every
    worker should succeed and produce its own per-issue worktree
    deterministically.
    """

    def setUp(self) -> None:
        self._repo = _RealGitWorktreeRepo()
        self._repo.prepare(self)

    def test_same_root_ensure_worktree_serialized(self) -> None:
        # Six concurrent workers, each requesting their own per-issue
        # worktree. With the lock in place all six must succeed; without
        # the lock at least one would intermittently surface
        # `error: could not lock config file .git/config: File exists`.
        recorder = _EnsureRecorder(self._repo._spec)
        _run_ensure_workers(self, recorder, ISSUE_NUMBERS)
        _assert_ensure_outcomes(self, recorder, ISSUE_NUMBERS)
        _assert_worktrees_exist(self, recorder)


if __name__ == "__main__":
    unittest.main()
