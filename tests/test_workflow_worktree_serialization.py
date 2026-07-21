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
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import (
    base_sync,
    config,
    git_plumbing,
    worktree_lifecycle,
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
        finally:
            with self._lock:
                self._in_flight -= 1
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
        # Clear the module-level lock dict so tests do not leak per-key
        # locks across runs (a stale lock from a previous test pointing
        # at a deleted tmp dir would still satisfy the API but would
        # spuriously serialize against a different test's lookup key).
        worktrees._TARGET_ROOT_LOCKS.clear()
        # Sanity: the guard lock itself is recreated, not reused. Tests
        # do not need a fresh guard lock but `clear()` empties the dict
        # under the existing guard, which is fine.
        self.assertIsInstance(
            worktrees._TARGET_ROOT_LOCKS_LOCK,
            type(threading.Lock()),
        )

    def test_root_lock_serializes_callers(self) -> None:
        # Drive `_ensure_worktree` against the SAME `spec.target_root`
        # from multiple threads with a `_git` patch that records every
        # subprocess invocation's concurrency. With the lock in place,
        # max-in-flight against target_root must be 1; without it, the
        # threads would interleave their git calls and trip an
        # assertion here.
        target_root = Path("/tmp/orchestrator-test-shared-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch=BASE_BRANCH,
        )
        probe = _ConcurrencyProbe(delay=PROBE_DELAY_SECONDS)

        with (
            patch.object(worktree_lifecycle, "_git", side_effect=probe.git),
            patch.object(
                worktree_lifecycle, "_authed_target_fetch",
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
        target_root = Path("/tmp/orchestrator-test-authed-fetch-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch=BASE_BRANCH,
        )
        wt = Path("/tmp/orchestrator-test-authed-fetch-worktree")

        probe = _ConcurrencyProbe(delay=PROBE_DELAY_SECONDS)

        # `_resolve_github_token` must return non-empty so `_authed_fetch`
        # does not short-circuit before the lock.
        with patch.object(
            config, "_resolve_github_token", return_value="ghp-test",
        ), patch.object(
            git_plumbing.subprocess,
            "run",
            side_effect=probe.subprocess_run,
        ):
            threads = [
                threading.Thread(
                    target=worktrees._authed_fetch,
                    args=(
                        spec,
                        f"+refs/heads/{BASE_BRANCH}:refs/remotes/"
                        f"{ORIGIN_REMOTE}/{BASE_BRANCH}",
                    ),
                    kwargs={"cwd": wt},
                )
                for _index in range(4)
            ]
            _start_and_join(threads, timeout=THREAD_TIMEOUT_SECONDS)
            for thread in threads:
                self.assertFalse(thread.is_alive())

        self.assertEqual(
            probe.maximum_in_flight,
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
                worktree_lifecycle, "_authed_target_fetch",
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
        # Fresh lock dict per test so a leftover entry pointing at a
        # previously-deleted tmp dir cannot satisfy a lookup and
        # accidentally serialize against an unrelated path.
        worktrees._TARGET_ROOT_LOCKS.clear()

        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-ensure-real-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            [GIT_COMMAND, "init", "--bare", "-b", BASE_BRANCH, str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            [GIT_COMMAND, "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "README.md").write_text("hello\n")
        _run_git("add", ".", cwd=self.work)
        _run_git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        _run_git("push", ORIGIN_REMOTE, BASE_BRANCH, cwd=self.work)

        # Point WORKTREES_DIR at our tmp dir for the duration of the test
        # so `_repo_worktrees_root` creates worktrees here, not in the
        # operator's real worktree dir.
        self._wd_patch = patch.object(
            config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        )
        self._wd_patch.start()
        self.addCleanup(self._wd_patch.stop)

        self.spec = config.RepoSpec(
            slug="acme/widget", target_root=self.work, base_branch=BASE_BRANCH,
            remote_name=ORIGIN_REMOTE,
        )

        self._fetch_patch = patch.object(
            base_sync, "_authed_target_fetch", side_effect=_local_fetch,
        )
        self._fetch_patch.start()
        self.addCleanup(self._fetch_patch.stop)

    def test_same_root_ensure_worktree_serialized(self) -> None:
        # Six concurrent workers, each requesting their own per-issue
        # worktree. With the lock in place all six must succeed; without
        # the lock at least one would intermittently surface
        # `error: could not lock config file .git/config: File exists`.
        issue_numbers = list(range(1, 7))
        recorder = _EnsureRecorder(self.spec)
        threads = [
            threading.Thread(target=recorder, args=(issue_number,))
            for issue_number in issue_numbers
        ]
        _start_and_join(threads, timeout=REAL_GIT_TIMEOUT_SECONDS)
        for thread in threads:
            self.assertFalse(
                thread.is_alive(), "worker timed out (possible lock contention)",
            )

        # No worker raised; every requested worktree path exists on disk.
        errors = [
            (number, error)
            for number, _, error in recorder.outcomes
            if error is not None
        ]
        self.assertEqual(
            errors, [],
            f"concurrent _ensure_worktree raised: {errors!r}",
        )
        self.assertEqual(
            sorted(number for number, _, _ in recorder.outcomes),
            issue_numbers,
        )
        for issue_number, worktree, _ in recorder.outcomes:
            self.assertIsNotNone(worktree)
            self.assertTrue(
                worktree.exists(),
                f"worktree {worktree} missing for issue #{issue_number}",
            )


if __name__ == "__main__":
    unittest.main()
