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
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import (
    base_sync,
    config,
    git_plumbing,
    worktree_lifecycle,
    worktrees,
)


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
        import threading
        worktrees._TARGET_ROOT_LOCKS.clear()
        # Sanity: the guard lock itself is recreated, not reused. Tests
        # do not need a fresh guard lock but `clear()` empties the dict
        # under the existing guard, which is fine.
        self.assertIsInstance(worktrees._TARGET_ROOT_LOCKS_LOCK, type(threading.Lock()))

    def test_target_root_lock_serializes_concurrent_callers(self) -> None:
        # Drive `_ensure_worktree` against the SAME `spec.target_root`
        # from multiple threads with a `_git` patch that records every
        # subprocess invocation's concurrency. With the lock in place,
        # max-in-flight against target_root must be 1; without it, the
        # threads would interleave their git calls and trip an
        # assertion here.
        import threading
        from unittest.mock import MagicMock

        target_root = Path("/tmp/orchestrator-test-shared-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch="main",
        )

        in_flight = 0
        max_in_flight = 0
        order: list[str] = []
        lock = threading.Lock()

        def fake_git(*args, cwd) -> MagicMock:
            # Every `_git(...)` call against this target_root counts -- a
            # `worktree add` is just one of several plumbing operations
            # that all share `.git/config.lock`.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(f"{args[0]}({threading.get_ident()})")
            # Sleep so threads piling up on the same target_root actually
            # overlap if the lock isn't holding them.
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            # Mimic `subprocess.CompletedProcess` enough for the helper:
            # returncode=0 for everything, plus `.stderr=""` /
            # `.stdout=""` defaults via MagicMock auto-attrs.
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_authed_fetch(spec, branch):
            # The base-branch fetch also runs under the lock; count it
            # the same way so the serialization assertion holds.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(f"fetch({threading.get_ident()})")
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_has_new_commits(*_a, **_kw) -> bool:
            return False  # force the "(re)create" branch every time.

        def call_ensure(n: int) -> None:
            worktrees._ensure_worktree(spec, n)

        with patch.object(worktree_lifecycle, "_git", side_effect=fake_git), \
             patch.object(
                 worktree_lifecycle, "_authed_target_fetch",
                 side_effect=fake_authed_fetch,
             ), \
             patch.object(worktree_lifecycle, "_has_new_commits", fake_has_new_commits), \
             patch.object(Path, "exists", lambda self: False), \
             patch.object(Path, "mkdir", lambda self, **_kw: None):
            threads = [
                threading.Thread(target=call_ensure, args=(n,))
                for n in (1, 2, 3, 4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive(), "worker timed out")

        # Every `_git` invocation against this target_root was serialized:
        # the per-target_root lock kept max-in-flight at 1 despite four
        # concurrent callers.
        self.assertEqual(
            max_in_flight, 1,
            f"git plumbing was not serialized; observed order={order!r}",
        )
        # And we actually drove the workers (sanity check).
        self.assertGreaterEqual(len(order), 4)

    def test_authed_fetch_is_serialized_per_target_root(self) -> None:
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
        import threading
        from unittest.mock import MagicMock

        target_root = Path("/tmp/orchestrator-test-authed-fetch-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch="main",
        )
        wt = Path("/tmp/orchestrator-test-authed-fetch-worktree")

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        # Track ONLY the `git fetch ...` call (not the pre-flight
        # `git config --local --get-regexp ...` check, which runs
        # outside the target_root lock on the worktree's own config).
        def fake_subprocess_run(args, **_kw) -> MagicMock:
            nonlocal in_flight, max_in_flight
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if len(args) >= 2 and args[-2:] != ["fetch", "--quiet"]:
                # The fetch invocation is `[git_prefix..., "fetch",
                # "--quiet", auth_url, refspec]`. Match on "fetch" being
                # present after the `git` binary + `-c` flags.
                pass
            if "fetch" in args and "--quiet" in args:
                with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                time.sleep(0.02)
                with lock:
                    in_flight -= 1
            return r

        # `_resolve_github_token` must return non-empty so `_authed_fetch`
        # does not short-circuit before the lock.
        with patch.object(
            config, "_resolve_github_token", return_value="ghp-test",
        ), patch.object(git_plumbing.subprocess, "run", side_effect=fake_subprocess_run):
            threads = [
                threading.Thread(
                    target=lambda i=i: worktrees._authed_fetch(
                        spec,
                        "+refs/heads/main:refs/remotes/origin/main",
                        cwd=wt,
                    ),
                )
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive())

        self.assertEqual(
            max_in_flight, 1,
            "_authed_fetch did not serialize concurrent fetches against "
            "the same target_root; the resolving_conflict handler would "
            "race on refs/remotes/<remote>/<base> lock files",
        )

    def test_different_target_roots_run_in_parallel(self) -> None:
        # Per-repo locks are keyed on `target_root`. Two specs pointing at
        # DIFFERENT target_roots must NOT serialize against each other --
        # otherwise the multi-repo loop would lose all parallelism.
        import threading
        from unittest.mock import MagicMock

        spec_a = config.RepoSpec(
            slug="acme/one",
            target_root=Path("/tmp/orchestrator-test-target-root-A"),
            base_branch="main",
        )
        spec_b = config.RepoSpec(
            slug="acme/two",
            target_root=Path("/tmp/orchestrator-test-target-root-B"),
            base_branch="main",
        )

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()
        # Block both threads inside `fake_git` simultaneously; if the
        # locks WERE shared across target_roots, one of the threads
        # would queue and the barrier would time out.
        barrier = threading.Barrier(2, timeout=5.0)

        def fake_git(*args, cwd) -> MagicMock:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            barrier.wait()
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_authed_fetch(spec, branch) -> MagicMock:
            # `_ensure_worktree` calls the authed fetch first; route it
            # through the same barrier so the in-flight count is built
            # from the fetch in each thread.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            barrier.wait()
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_has_new_commits(*_a, **_kw) -> bool:
            return False

        with patch.object(worktree_lifecycle, "_git", side_effect=fake_git), \
             patch.object(
                 worktree_lifecycle, "_authed_target_fetch",
                 side_effect=fake_authed_fetch,
             ), \
             patch.object(worktree_lifecycle, "_has_new_commits", fake_has_new_commits), \
             patch.object(Path, "exists", lambda self: False), \
             patch.object(Path, "mkdir", lambda self, **_kw: None):
            t_a = threading.Thread(
                target=lambda: worktrees._ensure_worktree(spec_a, 1)
            )
            t_b = threading.Thread(
                target=lambda: worktrees._ensure_worktree(spec_b, 1)
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=10.0)
            t_b.join(timeout=10.0)
            self.assertFalse(t_a.is_alive())
            self.assertFalse(t_b.is_alive())

        # Both threads cleared the barrier together, so they were
        # genuinely in-flight at the same moment.
        self.assertEqual(max_in_flight, 2)


class EnsureWorktreeRealGitConcurrencyTest(unittest.TestCase):
    """Integration smoke test for the per-target_root lock: drive multiple
    real `_ensure_worktree` calls against a real bare remote concurrently.

    Without the lock, even at 2 workers `git worktree add` would
    intermittently report `error: could not lock config file .git/config:
    File exists` (the reviewer's reproducer). With the lock, every
    worker should succeed and produce its own per-issue worktree
    deterministically.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        # Fresh lock dict per test so a leftover entry pointing at a
        # previously-deleted tmp dir cannot satisfy a lookup and
        # accidentally serialize against an unrelated path.
        worktrees._TARGET_ROOT_LOCKS.clear()

        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-ensure-real-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Point WORKTREES_DIR at our tmp dir for the duration of the test
        # so `_repo_worktrees_root` creates worktrees here, not in the
        # operator's real worktree dir.
        self._wd_patch = patch.object(
            config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        )
        self._wd_patch.start()
        self.addCleanup(self._wd_patch.stop)

        self.spec = config.RepoSpec(
            slug="acme/widget", target_root=self.work, base_branch="main",
            remote_name="origin",
        )

        # `_authed_target_fetch` dials `https://x-access-token@github.com/...`
        # which has no answer for our local bare remote. Redirect to a
        # plain local fetch so the test still exercises the
        # `_ensure_worktree` worktree-add concurrency path.
        def _local_fetch(spec, branch):
            return subprocess.run(
                ["git", "fetch", "--quiet", spec.remote_name, branch],
                cwd=str(spec.target_root),
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

        self._fetch_patch = patch.object(
            base_sync, "_authed_target_fetch", side_effect=_local_fetch,
        )
        self._fetch_patch.start()
        self.addCleanup(self._fetch_patch.stop)

    def test_concurrent_ensure_worktree_against_same_target_root(self) -> None:
        # Six concurrent workers, each requesting their own per-issue
        # worktree. With the lock in place all six must succeed; without
        # the lock at least one would intermittently surface
        # `error: could not lock config file .git/config: File exists`.
        import threading
        results: list[tuple[int, Optional[Path], Optional[BaseException]]] = []
        results_lock = threading.Lock()

        def call_ensure(n: int) -> None:
            try:
                wt = worktrees._ensure_worktree(self.spec, n)
                with results_lock:
                    results.append((n, wt, None))
            except BaseException as e:  # noqa: BLE001 - record for assertion
                with results_lock:
                    results.append((n, None, e))

        issue_numbers = list(range(1, 7))
        threads = [
            threading.Thread(target=call_ensure, args=(n,))
            for n in issue_numbers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
            self.assertFalse(
                t.is_alive(), "worker timed out (possible lock contention)",
            )

        # No worker raised; every requested worktree path exists on disk.
        errors = [(n, e) for n, _, e in results if e is not None]
        self.assertEqual(
            errors, [],
            f"concurrent _ensure_worktree raised: {errors!r}",
        )
        self.assertEqual(sorted(n for n, _, _ in results), issue_numbers)
        for n, wt, _ in results:
            self.assertIsNotNone(wt)
            self.assertTrue(wt.exists(), f"worktree {wt} missing for issue #{n}")


if __name__ == "__main__":
    unittest.main()
