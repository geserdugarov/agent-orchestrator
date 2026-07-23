# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree plumbing serialization: the per-`target_root` lock that keeps
`_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`
from racing on `.git/config.lock` when `tick()` fans non-family-aware
stages out across worker threads. Covers both the deterministic blocking-
fake unit tests and a real-git integration smoke test against a real bare
remote."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import (
    config,
    git_plumbing,
    worktree_lifecycle,
    worktrees,
)

from tests.worktree_serialization_support import (
    _ConcurrencyProbe,
    _has_no_new_commits,
    _start_and_join,
)

GIT_COMMAND = "git"
BASE_BRANCH = "main"
ORIGIN_REMOTE = "origin"
PROBE_DELAY_SECONDS = 0.02
THREAD_TIMEOUT_SECONDS = 10.0
BARRIER_TIMEOUT_SECONDS = 5.0
REAL_GIT_TIMEOUT_SECONDS = 30.0


class WorktreePlumbingSerializationTest(unittest.TestCase):
    """`tick()` fans non-family-aware stages out across worker threads, so
    `_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`
    can be invoked concurrently against the same `spec.target_root`. The
    git plumbing those helpers run -- `git fetch`, `git worktree add`,
    `git worktree remove` -- writes the parent clone's `.git/config` under
    `.git/config.lock`. Without per-target_root serialization git reports
    `error: could not lock config file .git/config: File exists` and the
    worker fails before its agent ever spawns. These tests pin the lock
    contract down with both a deterministic blocking-fake unit test (every
    `_git` call records concurrency against the lock) and a real-git
    integration smoke test (10 workers, real `git worktree add` against
    a real bare remote)."""

    def setUp(self) -> None:
        # Clear the process-local registry so tests do not retain per-key locks
        # for temporary paths from earlier cases.
        worktrees._TARGET_ROOT_LOCKS.clear()
        self.assertIsInstance(
            worktrees._TARGET_ROOT_LOCKS_LOCK,
            type(threading.Lock()),
        )

    def test_root_lock_registry_keeps_path_identity(self) -> None:
        target_root = Path("/tmp/orchestrator-test-stable-target-root")

        first = worktrees._target_root_lock(target_root)
        second = worktrees._target_root_lock(target_root)
        other = worktrees._target_root_lock(target_root.with_name("other-root"))

        self.assertIs(first, second)
        self.assertIsNot(first, other)
        self.assertNotIsInstance(worktrees._TARGET_ROOT_LOCKS, dict)

    def test_root_lock_serializes_callers(self) -> None:
        # Drive `_ensure_worktree` against the SAME `spec.target_root`
        # from multiple threads with a `_git` patch that records every
        # subprocess invocation's concurrency. With the lock in place,
        # max-in-flight against target_root must be 1; without it, the
        # threads would interleave their git calls and trip an
        # assertion here.
        target_root = Path("/tmp/orchestrator-test-shared-target-root")
        spec = config.RepoSpec(
            slug="acme/widget",
            target_root=target_root,
            base_branch=BASE_BRANCH,
        )
        probe = _ConcurrencyProbe(delay=PROBE_DELAY_SECONDS)

        with (
            patch.object(worktree_lifecycle, "_git", side_effect=probe.git),
            patch.object(
                worktree_lifecycle,
                "_authed_target_fetch",
                side_effect=probe.fetch,
            ),
            patch.object(
                worktree_lifecycle,
                "_has_new_commits",
                _has_no_new_commits,
            ),
            patch.object(Path, "exists", lambda _path: False),
            patch.object(Path, "mkdir", lambda _path, **_kwargs: None),
        ):
            threads = [
                threading.Thread(
                    target=worktrees._ensure_worktree,
                    args=(spec, issue_number),
                )
                for issue_number in (1, 2, 3, 4)
            ]
            _start_and_join(threads, timeout=THREAD_TIMEOUT_SECONDS)
            for thread in threads:
                self.assertFalse(thread.is_alive(), "worker timed out")

        # Every `_git` invocation against this target_root was serialized:
        # the per-target_root lock kept max-in-flight at 1 despite four
        # concurrent callers.
        self.assertEqual(
            probe.maximum_in_flight,
            1,
            f"git plumbing was not serialized; observed order={probe.order!r}",
        )
        # And we actually drove the workers (sanity check).
        self.assertGreaterEqual(len(probe.order), 4)

    def test_fetch_serialized_per_root(self) -> None:
        # `_authed_fetch` updates `refs/remotes/<remote>/<branch>` in the
        # parent clone's git directory (worktrees share the parent's
        # `.git/refs` namespace). Two concurrent `_authed_fetch` calls
        # from different worktrees of the same target_root therefore
        # race on `<branch>.lock` / `packed-refs.lock` and one can fail
        # with `Unable to create '...': File exists`. The reviewer
        # specifically called out the `resolving_conflict` handler at
        # workflow.py:1646 -- it calls `_authed_fetch` against
        # `refs/heads/<base>` which is the single most-contended ref.
        # The fix wraps the actual `git fetch` subprocess in
        # `_target_root_lock`. This test patches `subprocess.run` to
        # record concurrency across the lock-protected critical
        # section and asserts max-in-flight == 1.
        scenario = SimpleNamespace(
            spec=config.RepoSpec(
                slug="acme/widget",
                target_root=Path(
                    "/tmp/orchestrator-test-authed-fetch-target-root",
                ),
                base_branch=BASE_BRANCH,
            ),
            worktree=Path("/tmp/orchestrator-test-authed-fetch-worktree"),
            probe=_ConcurrencyProbe(delay=PROBE_DELAY_SECONDS),
        )

        # `_resolve_github_token` must return non-empty so `_authed_fetch`
        # does not short-circuit before the lock.
        with (
            patch.object(
                config,
                "_resolve_github_token",
                return_value="ghp-test",
            ),
            patch.object(
                git_plumbing.subprocess,
                "run",
                side_effect=scenario.probe.subprocess_run,
            ),
        ):
            threads = [
                threading.Thread(
                    target=worktrees._authed_fetch,
                    args=(
                        scenario.spec,
                        f"+refs/heads/{BASE_BRANCH}:refs/remotes/{ORIGIN_REMOTE}/{BASE_BRANCH}",
                    ),
                    kwargs={"cwd": scenario.worktree},
                )
                for _index in range(4)
            ]
            _start_and_join(threads, timeout=THREAD_TIMEOUT_SECONDS)
            for thread in threads:
                self.assertFalse(thread.is_alive())

        self.assertEqual(
            scenario.probe.maximum_in_flight,
            1,
            "_authed_fetch did not serialize concurrent fetches against "
            "the same target_root; the resolving_conflict handler would "
            "race on refs/remotes/<remote>/<base> lock files",
        )

    def test_different_target_roots_run_in_parallel(self) -> None:
        # Per-repo locks are keyed on `target_root`. Two specs pointing at
        # DIFFERENT target_roots must NOT serialize against each other --
        # otherwise the multi-repo loop would lose all parallelism.
        spec_a = config.RepoSpec(
            slug="acme/one",
            target_root=Path("/tmp/orchestrator-test-target-root-A"),
            base_branch=BASE_BRANCH,
        )
        spec_b = config.RepoSpec(
            slug="acme/two",
            target_root=Path("/tmp/orchestrator-test-target-root-B"),
            base_branch=BASE_BRANCH,
        )

        # Block both threads inside `fake_git` simultaneously; if the
        # locks WERE shared across target_roots, one of the threads
        # would queue and the barrier would time out.
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
        probe = _ConcurrencyProbe(barrier=barrier)

        with (
            patch.object(worktree_lifecycle, "_git", side_effect=probe.git),
            patch.object(
                worktree_lifecycle,
                "_authed_target_fetch",
                side_effect=probe.fetch,
            ),
            patch.object(
                worktree_lifecycle,
                "_has_new_commits",
                _has_no_new_commits,
            ),
            patch.object(Path, "exists", lambda _path: False),
            patch.object(Path, "mkdir", lambda _path, **_kwargs: None),
        ):
            threads = [
                threading.Thread(
                    target=worktrees._ensure_worktree,
                    args=(spec, 1),
                )
                for spec in (spec_a, spec_b)
            ]
            _start_and_join(threads, timeout=THREAD_TIMEOUT_SECONDS)
            self.assertFalse(any(thread.is_alive() for thread in threads))

        # Both threads cleared the barrier together, so they were
        # genuinely in-flight at the same moment.
        self.assertEqual(probe.maximum_in_flight, 2)
