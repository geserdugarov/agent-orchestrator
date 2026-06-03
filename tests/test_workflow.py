# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import (
    analytics,
    base_sync,
    config,
    git_plumbing,
    workflow,
    worktree_lifecycle,
    worktrees,
)
from orchestrator.github import BACKLOG_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
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


class BacklogLabelSkipsProcessingTest(unittest.TestCase):
    """The `backlog` control label is a "not yet" hold: applied to an issue
    (typically a freshly opened one), it prevents the orchestrator from
    decomposing, picking up, or otherwise advancing the state machine until
    a human removes the label.
    """

    def test_unlabeled_issue_with_backlog_skips_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701)
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_in_flight_issue_with_backlog_skips_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(702, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        impl.assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_removing_backlog_allows_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(703)
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_called_once_with(gh, _TEST_SPEC, issue)


class QuestionLabelRoutingTest(unittest.TestCase):
    """`question` is a first-class workflow label routed to its own stage
    handler. The behavioral tests for that handler live in
    `tests/test_workflow_question.py`; this class only covers label
    bootstrapping and dispatcher routing.
    """

    def test_question_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("question", WORKFLOW_LABELS)

    def test_question_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("question", names)

    def test_question_label_is_not_family_aware(self) -> None:
        # Open `question` issues touch only their own pinned state, so the
        # label must stay out of `_FAMILY_AWARE_LABELS` -- otherwise the
        # parallel tick path would route it through the single-threaded
        # family bucket and defeat fan-out concurrency.
        self.assertNotIn("question", workflow._FAMILY_AWARE_LABELS)

    def test_dispatcher_routes_question_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(801, label="question")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_question") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()


class DocumentingLabelRoutingTest(unittest.TestCase):
    """`documenting` is registered as a workflow label so the dispatcher
    routes it to the stub stage handler instead of falling through to
    pickup or implementation. The implementing stage does not auto-apply
    this label yet (parent #149), so any issue carrying it arrived via a
    manual operator action -- the stub parks awaiting human rather than
    silently skipping, otherwise the issue would sit forever waiting for a
    non-existent handler to advance it.
    """

    def test_documenting_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("documenting", WORKFLOW_LABELS)

    def test_documenting_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("documenting", names)

    def test_documenting_label_sits_between_validating_and_in_review(
        self,
    ) -> None:
        # The happy-path lifecycle is implementing -> validating ->
        # documenting (final-docs hop) -> in_review; the spec tuple
        # places the labels in roughly that order so a reader scanning
        # WORKFLOW_LABEL_SPECS top-to-bottom sees the actual flow.
        # Lifecycle routing itself lives in the stage handlers, not
        # this tuple, but the order shouldn't actively mislead.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        impl_idx = names.index("implementing")
        val_idx = names.index("validating")
        doc_idx = names.index("documenting")
        in_review_idx = names.index("in_review")
        self.assertEqual(val_idx, impl_idx + 1)
        self.assertEqual(doc_idx, val_idx + 1)
        self.assertEqual(in_review_idx, doc_idx + 1)

    def test_documenting_label_is_not_family_aware(self) -> None:
        # Open `documenting` issues touch only their own pinned state and
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS`
        # -- otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("documenting", workflow._FAMILY_AWARE_LABELS)

    def test_documenting_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh. The brief final-
        # docs hop is PR-having (its sibling labels validating /
        # in_review / fixing already qualify), and the documenting
        # handler only checks ahead/behind vs. the PR branch -- not
        # base -- so without the detour a sibling-PR merge during the
        # docs pass would leave the docs commit on a stale base and
        # only the next in_review tick would catch it. Including the
        # label here is what keeps `hold_base_sync` as the only label
        # that gates auto-rebase for a PR-stage worktree.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("documenting", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_documenting_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(901, label="documenting")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_documenting") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_validating") as val:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        val.assert_not_called()

    def test_documenting_without_pr_number_parks_awaiting_human(self) -> None:
        # End-to-end with the real handler: a manually-applied
        # `documenting` label on an issue with no pinned `pr_number`
        # cannot anchor on a dev PR worktree, so the handler parks
        # awaiting human rather than guessing.
        gh = FakeGitHubClient()
        issue = make_issue(902, label="documenting")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 902)
        self.assertIn("documenting", body)
        self.assertTrue(gh.pinned_data(902).get("awaiting_human"))
        # The label is NOT flipped: parking surfaces the situation but
        # leaves the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_documenting_missing_pr_number_is_idempotent_when_parked(
        self,
    ) -> None:
        # A second tick on an already-parked documenting issue (still
        # missing `pr_number`) must not re-post the parking comment or
        # re-emit the audit event -- otherwise every polling tick
        # would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(903, label="documenting")
        gh.add_issue(issue)
        gh.seed_state(903, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)


class FixingLabelRoutingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`fixing` is registered as a workflow label that sits between
    `in_review` and `validating` in the PR-feedback fix loop. The dispatcher
    must route the label to `_handle_fixing` instead of falling through to
    pickup or implementation, and the bootstrap specs / family-aware
    partitioning / closed-issue sweep / PR-worktree refresh detour must
    all recognise it as a PR-having stage. The PR-terminal arcs and the
    no-`pr_number` park covered here pair with the quiet-window / dev-
    resume tests in `tests/test_workflow_fixing.py`.
    """

    def test_fixing_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("fixing", WORKFLOW_LABELS)

    def test_fixing_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("fixing", names)

    def test_fixing_label_sits_between_in_review_and_resolving_conflict(
        self,
    ) -> None:
        # Lifecycle order matters: `fixing` is the next stage after
        # `in_review` when the PR has fresh feedback. The spec tuple
        # encodes the lifecycle ordering, so it must place `fixing` right
        # after `in_review`.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        in_review_idx = names.index("in_review")
        fixing_idx = names.index("fixing")
        self.assertEqual(fixing_idx, in_review_idx + 1)

    def test_fixing_label_is_not_family_aware(self) -> None:
        # Open `fixing` issues touch only their own pinned state and PR
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS` --
        # otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("fixing", workflow._FAMILY_AWARE_LABELS)

    def test_fixing_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh; a `fixing` worktree
        # is PR-having (its sibling labels validating/in_review already
        # qualify) so it must be eligible for the same detour.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("fixing", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_fixing_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="fixing")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_fixing") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_in_review") as in_review:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        in_review.assert_not_called()

    def test_fixing_without_pr_number_parks_awaiting_human(self) -> None:
        # A manual relabel directly to `fixing` without a recorded
        # `pr_number` cannot drive the dev-resume path (no PR to push
        # against). Park once, surfacing the misconfiguration to a
        # human; the label is left in place so the operator can fix
        # the relabel.
        gh = FakeGitHubClient()
        issue = make_issue(702, label="fixing")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 702)
        self.assertIn("fixing", body)
        self.assertIn("pr_number", body)
        self.assertTrue(gh.pinned_data(702).get("awaiting_human"))
        # The `reason="missing_pr_number"` is recorded on the audit
        # event by `_park_awaiting_human`; the durable `park_reason`
        # field stays None (callers that need a transient/recoverable
        # tag re-set it explicitly -- this park is HITL-only).
        events_for_issue = [
            e for e in gh.recorded_events
            if e.get("issue") == 702
            and e.get("event") == "park_awaiting_human"
        ]
        self.assertEqual(len(events_for_issue), 1)
        self.assertEqual(events_for_issue[0].get("reason"), "missing_pr_number")
        # The label stays put: parking surfaces the situation but leaves
        # the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_fixing_without_pr_number_is_idempotent_when_already_parked(
        self,
    ) -> None:
        # A second tick on an already-parked no-PR fixing issue must
        # not re-post the parking comment -- otherwise every polling
        # tick would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(703, label="fixing")
        gh.add_issue(issue)
        gh.seed_state(703, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_fixing_skips_closed_issue_without_pr_number(self) -> None:
        # A closed-`fixing` issue with no recorded PR (manual relabel from
        # an early stage, no PR opened) cannot be finalized via the
        # PR-state arcs. The handler must NOT park (parking a closed issue
        # would spam a parking comment on a terminated thread); it leaves
        # the label alone and lets the operator relabel manually.
        gh = FakeGitHubClient()
        issue = make_issue(704, label="fixing")
        issue.closed = True
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])

    def test_fixing_finalizes_closed_issue_on_external_merge(self) -> None:
        # The headline closed-sweep contract: a human merges the PR with
        # `Resolves #N` while the issue is labeled `fixing`. The issue
        # auto-closes; the closed-issue sweep yields it; the handler must
        # finalize to `done`, stamp `merged_at`, close (already closed),
        # and run branch cleanup -- otherwise the issue sits closed +
        # `fixing` forever.
        gh = FakeGitHubClient()
        issue = make_issue(705, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=801, head_branch="orchestrator/issue-705",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(705, pr_number=pr.number, branch="orchestrator/issue-705")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((705, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(705))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 705,
        )

    def test_fixing_finalizes_closed_issue_on_closed_without_merge(
        self,
    ) -> None:
        # Mirror branch: PR was closed without merging while the issue
        # was in `fixing`. Handler must flip to `rejected`, stamp
        # `closed_without_merge_at`, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(706, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=802, head_branch="orchestrator/issue-706",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(706, pr_number=pr.number, branch="orchestrator/issue-706")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((706, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(706))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 706,
        )

    def test_closed_fixing_issue_surfaces_in_pollable_sweep(self) -> None:
        # The closed-issue sweep has to include `fixing` so the handler
        # can finalize an externally-merged PR to `done` even when
        # `Resolves #N` already closed the issue.
        gh = FakeGitHubClient()
        open_impl = make_issue(710, label="implementing")
        closed_fixing = make_issue(711, label="fixing")
        closed_fixing.closed = True
        for i in (open_impl, closed_fixing):
            gh.add_issue(i)

        numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(numbers, {710, 711})

    def test_auto_merge_does_not_fire_while_label_is_fixing(self) -> None:
        # Headline merge-safeguard contract: an approved + mergeable PR
        # whose linked issue is labeled `fixing` MUST NOT produce any
        # `gh.merge_pr` call. The orchestrator is permanently manual-
        # merge-only -- no handler calls `merge_pr` today -- but the
        # dispatcher also routes `fixing` to `_handle_fixing` (not
        # `_handle_in_review`), so a regression that smuggled a merge
        # call back into in_review would still not fire here. The
        # `merge_calls == []` assertion below catches either drift.
        gh = FakeGitHubClient()
        issue = make_issue(720, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=901, head_branch="orchestrator/issue-720",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            720, pr_number=pr.number,
            branch="orchestrator/issue-720",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Pending feedback recorded by the prior in_review tick.
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
        )

        self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge call, no flip to done -- the dispatcher routed to
        # fixing, so the in_review merge path never ran.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((720, "done"), gh.label_history)


class FixingConflictDetourTest(unittest.TestCase):
    """A behind-base `fixing` worktree is detoured into
    `resolving_conflict` by the pre-tick refresh. The detour must NOT
    swallow pending PR feedback: the `pending_fix_*` bookmarks recorded
    by the in_review handoff and the in_review watermarks MUST survive
    the relabel, so the eventual return from `resolving_conflict` ->
    `validating` -> `in_review` re-discovers the unread feedback and
    routes it back to `fixing`.
    """

    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target-fixing"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt-fixing")
        self.gh = FakeGitHubClient()

    def _git_result(
        self, *, returncode: int = 0, stdout: str = ""
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
        )

    def test_fixing_detour_preserves_pending_feedback(self) -> None:
        # A `fixing` worktree that is N commits behind `origin/<base>`
        # must flip to `resolving_conflict` and PRESERVE the
        # `pending_fix_*` bookmarks and `pr_last_comment_id` watermark.
        # Any bump of those values here would silently consume the
        # unread feedback that triggered the original in_review ->
        # fixing route: when the resolving_conflict handler eventually
        # pushes the rebase and the validating -> in_review handoff
        # runs, the rescan would skip the (now-watermarked-past) human
        # comment and the in_review HITL ready-ping could advertise
        # the PR as ready for human merge over it.
        self.gh.add_issue(make_issue(7, label="fixing"))
        pr = FakePR(
            number=42, head_branch="orchestrator/issue-7",
            head=FakePRRef(sha="cafe1234"),
            state="open",
        )
        self.gh.add_pr(pr)
        self.gh.seed_state(
            7,
            pr_number=42,
            branch="orchestrator/issue-7",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
            pending_fix_review_max_id=3000,
            pending_fix_review_summary_max_id=4000,
        )
        # Behind base by 3 commits drives the detour.
        git_mock = patch.object(
            base_sync, "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             git_mock:
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        # Detour fired: label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # Pending-fix bookmarks survived the relabel so the eventual
        # in_review re-entry can correlate the triggering ids.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("pending_fix_at"), "2026-05-23T00:00:00+00:00")
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        self.assertEqual(data.get("pending_fix_review_max_id"), 3000)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4000)
        # And the in_review watermark is unchanged -- the rescan after
        # resolving_conflict -> validating -> in_review will surface
        # the original triggering comment as fresh feedback again.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)


class InReviewRoutesFreshFeedbackToFixingTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Fresh actionable PR feedback during `in_review` must hand the issue
    off to `fixing` immediately -- no debounce wait, no dev spawn from the
    in_review handler itself. The pending-fix bookmark recorded in pinned
    state gives the (future) fixing handler a starting point for the
    triggering comment.
    """

    PR_NUMBER = 880
    BRANCH = "orchestrator/issue-880"

    def _seed_in_review_with_pr(self, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(880, label="in_review")
        gh.add_issue(issue)
        if pr is None:
            pr = FakePR(
                number=self.PR_NUMBER, head_branch=self.BRANCH,
                head=FakePRRef(sha="cafe1234"),
                mergeable=True, check_state="success",
            )
        gh.add_pr(pr)
        state = dict(
            pr_number=pr.number,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(880, **state)
        return gh, issue, pr

    def test_fresh_pr_conversation_comment_flips_to_fixing_no_dev_spawn(
        self,
    ) -> None:
        # The headline contract: a single fresh PR conversation comment
        # within the debounce window must route the issue from `in_review`
        # to `fixing` on this tick. The dev is NOT spawned by
        # `_handle_in_review` any more -- the fixing stage owns that step.
        # Run through the full dispatcher (`_process_issue`) so the test
        # also covers the routing wiring end-to-end.
        now = datetime.now(timezone.utc)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000,
                    body="please tighten the integration test",
                    user=FakeUser("alice"),
                    created_at=now,  # well inside the debounce window
                ),
            ],
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev spawn during the debounce window (or after it -- the
        # in_review handler no longer spawns the dev at all).
        mocks["run_agent"].assert_not_called()
        # No merge attempt either: the orchestrator never merges and
        # the fresh feedback routes to fixing.
        self.assertEqual(gh.merge_calls, [])
        # The label flipped to `fixing` this tick.
        self.assertIn((880, "fixing"), gh.label_history)
        # Pending-fix metadata records the triggering comment id and an
        # ISO timestamp so the fixing handler has a bookmark.
        data = gh.pinned_data(880)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 3000)
        self.assertIn("pending_fix_at", data)
        # Watermark stays put so the fixing handler can rescan and reach
        # the triggering comment on its next tick.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)

    def test_no_fresh_feedback_pings_hitl_for_manual_merge(self) -> None:
        # The in_review -> fixing route must NOT preempt the mergeable /
        # HITL-ping path: an approved, mergeable, green PR with no fresh
        # PR comments earns a one-shot HITL ping (the orchestrator never
        # merges) and stays open.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge, no fixing route, no terminal flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertNotIn("pending_fix_at", gh.pinned_data(880))
        # HITL ping fired exactly once.
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(880).get("ready_ping_sha"), "cafe1234",
        )

    def test_no_fresh_feedback_preserves_pr_merged_terminal(self) -> None:
        # Existing terminal PR handling must still finalize the issue to
        # `done` on an external merge -- the fixing route is gated on
        # fresh PR feedback and must not preempt the merged-PR branch.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(880))

    def test_fresh_issue_thread_comment_routes_to_fixing_despite_drift_hash(
        self,
    ) -> None:
        # Regression test for the reviewer's reproducer: a normal fresh
        # issue-thread review comment used to trigger user-content drift
        # (because `user_content_hash` covers human issue comments) and
        # the drift path would `_resume_dev_with_text` + flip to
        # `validating` -- violating the contract that any fresh issue-
        # thread feedback during `in_review` records `pending_fix_*` and
        # routes to `fixing`. Seed a stale prior `user_content_hash` so
        # the drift path WOULD fire if the ordering were wrong, then
        # confirm the fresh-feedback scan wins.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh = FakeGitHubClient()
        issue = make_issue(1660, label="in_review")
        # Issue-thread comment posted after the watermark; the hash that
        # was recorded earlier did not include it, so the drift detector
        # WOULD fire on the next tick if the scan order were wrong.
        issue.comments.append(FakeComment(
            id=7000, body="please tighten the docstring",
            user=FakeUser("alice"), created_at=long_ago,
        ))
        gh.add_issue(issue)
        pr = FakePR(
            number=1661, head_branch="orchestrator/issue-1660",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            1660,
            pr_number=pr.number,
            branch="orchestrator/issue-1660",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=6999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Stale hash that doesn't cover the human comment above --
            # the drift path WOULD fire on this tick if the scan order
            # were wrong (this is the reviewer's reproducer).
            user_content_hash="stale-hash-from-before-the-human-comment",
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Contract: no dev spawn, no flip to `validating`.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((1660, "validating"), gh.label_history)
        # The issue routed to `fixing` and recorded the triggering
        # bookmark.
        self.assertIn((1660, "fixing"), gh.label_history)
        data = gh.pinned_data(1660)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 7000)
        self.assertIn("pending_fix_at", data)
        # And the hash was refreshed so the drift path does NOT
        # double-fire on the same comment changes after the fixing
        # handler (or an operator) bounces the issue back to `in_review`.
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-before-the-human-comment",
        )


class StageEvaluationAnalyticsTest(unittest.TestCase):
    """`_process_issue` times every dispatch and appends a single
    `stage_evaluation` analytics record carrying repo / issue / stage /
    duration_s / result. The record fires on both happy-path and
    exception paths; an unhandled handler exception still propagates so
    the per-issue tick try/except in `workflow.tick` keeps the legacy
    isolation behavior. Backlog-skips are NOT timed -- no handler runs.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_handler_success_appends_stage_evaluation_record(self) -> None:
        # End-to-end: a labeled issue runs through the dispatcher with
        # the matching handler mocked, and the wrapper writes one
        # `stage_evaluation` line carrying the current label + ok result.
        with tempfile.TemporaryDirectory(prefix="analytics-stageval-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8001, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8001
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
        self.assertEqual(rec["stage"], "implementing")
        self.assertEqual(rec["result"], "ok")
        self.assertIn("duration_s", rec)
        self.assertGreaterEqual(rec["duration_s"], 0)

    def test_unlabeled_issue_records_stage_evaluation_with_no_stage(
        self,
    ) -> None:
        # The dispatcher routes a label=None issue to `_handle_pickup`;
        # the `stage_evaluation` record drops the optional `stage` field
        # (build_record's documented contract for None values) so the
        # absence of a workflow label is encoded as "no stage" rather
        # than a string sentinel that downstream aggregations would
        # have to special-case.
        with tempfile.TemporaryDirectory(prefix="analytics-pickup-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8002)
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_pickup"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8002
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertNotIn("stage", rec)
        self.assertEqual(rec["result"], "ok")

    def test_handler_exception_records_error_result_and_propagates(
        self,
    ) -> None:
        # The handler raising must NOT suppress the exception: the
        # tick loop's per-issue isolation depends on the dispatcher
        # surfacing failures so they can be logged and the loop
        # continues with the next issue. The record must still land
        # with result=error and the duration captured up to the raise.
        sentinel = RuntimeError("handler blew up")
        with tempfile.TemporaryDirectory(prefix="analytics-err-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8003, label="validating")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(
                     workflow, "_handle_validating", side_effect=sentinel,
                 ):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow._process_issue(gh, _TEST_SPEC, issue)
                self.assertIs(ctx.exception, sentinel)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8003
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["stage"], "validating")
        self.assertEqual(rec["result"], "error")
        self.assertIn("duration_s", rec)

    def test_backlog_skip_does_not_record_stage_evaluation(self) -> None:
        # Backlog parks the issue OUTSIDE the state machine before any
        # handler runs; there is nothing to time. The early return must
        # short-circuit before the timing wrapper writes a record so
        # operators do not see a noisy run of zero-duration evaluations
        # for issues that the orchestrator deliberately ignores.
        with tempfile.TemporaryDirectory(prefix="analytics-backlog-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8004, label="implementing")
            issue.labels.append(FakeLabel(BACKLOG_LABEL))
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing") as handler:
                workflow._process_issue(gh, _TEST_SPEC, issue)
            handler.assert_not_called()
        self.assertEqual(self._records(path), [])

    def test_disabled_sink_does_not_write_evaluation_record(self) -> None:
        # The off knob is documented as a silent no-op for the analytics
        # sink. `_process_issue` must respect it so an operator who set
        # ANALYTICS_LOG_PATH=off does not see a phantom file appear.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8005, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])


class StageEnterAnalyticsRecordTest(unittest.TestCase):
    """`set_workflow_label` is the single chokepoint for stage transitions;
    every flip emits both the audit `stage_enter` event (to
    `EVENT_LOG_PATH`) and an analytics-compatible `stage_enter` record
    (to `ANALYTICS_LOG_PATH`). Workflow correctness still keys on pinned
    GitHub state; the analytics record is observability only.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_label_transition_writes_analytics_stage_enter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-stage-enter-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8101)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, "implementing")
                gh.set_workflow_label(issue, "validating")
            records = self._records(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [r["stage"] for r in records],
            ["implementing", "validating"],
        )
        for r in records:
            self.assertEqual(r["event"], "stage_enter")
            self.assertEqual(r["issue"], 8101)
            self.assertEqual(r["repo"], "geserdugarov/agent-orchestrator")
            datetime.fromisoformat(r["ts"])

    def test_label_cleared_to_none_does_not_emit_record(self) -> None:
        # Mirrors the existing `_emit_stage_enter` no-op for None labels:
        # clearing a label is not a stage and must not produce a phantom
        # `stage_enter` analytics record.
        with tempfile.TemporaryDirectory(prefix="analytics-stage-none-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8102, label="implementing")
                gh.add_issue(issue)
                gh.set_workflow_label(issue, None)
        self.assertEqual(self._records(path), [])


class FinalizeIfPrMergedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the cross-stage `_finalize_if_pr_merged` helper.

    Stages that previously had no merged-PR check (`_handle_implementing`,
    `_handle_documenting`, `_handle_validating`) plus the umbrella /
    blocked aggregation now call this helper to short-circuit a stale
    in-flight label when the linked PR was merged externally. The helper
    is the single chokepoint, so it carries its own tests in addition to
    the per-handler smoke tests.
    """

    def _state_with_pr_number(self, gh, issue_number, pr_number):
        from orchestrator.github import PinnedState
        gh.seed_state(issue_number, pr_number=pr_number)
        # Mirror what handlers do: read pinned state and hand it to the helper.
        state = PinnedState(comment_id=None, data={"pr_number": pr_number})
        return state

    def test_no_pr_number_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(200, label="validating")
        gh.add_issue(issue)
        from orchestrator.github import PinnedState

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, PinnedState()
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_open_pr_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20100, head_branch="orchestrator/issue-201",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 201, 20100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_unmerged_pr_returns_false(self) -> None:
        # Closed without merge is `rejected` territory; the helper covers
        # only the merged case so the in_review / fixing / resolving_conflict
        # handlers stay in charge of the rejected arc with their own
        # `closed_without_merge_at` stamp + `pr_closed_without_merge` event.
        gh = FakeGitHubClient()
        issue = make_issue(202, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20200, head_branch="orchestrator/issue-202",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 202, 20200)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_merged_pr_finalizes_open_issue(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(203, label="implementing")
        gh.add_issue(issue)
        pr = FakePR(
            number=20300, head_branch="orchestrator/issue-203",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 203, 20300)

        result = self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((203, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 203,
        )
        # An `external`-merge audit event is emitted with the
        # entry-stage label.
        kinds = [e["event"] for e in gh.recorded_events]
        self.assertIn("pr_merged", kinds)
        merged_event = next(
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        )
        self.assertEqual(merged_event.get("merge_method"), "external")
        self.assertEqual(merged_event.get("stage"), "implementing")

    def test_merged_pr_finalizes_closed_issue(self) -> None:
        # An externally-merged PR with `Resolves #N` auto-closes the issue
        # before the orchestrator can react. The helper must still
        # finalize the label (and not attempt to re-close).
        gh = FakeGitHubClient()
        issue = make_issue(204, label="validating")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=20400, head_branch="orchestrator/issue-204",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 204, 20400)

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((204, "done"), gh.label_history)
        self.assertTrue(issue.closed)


class DrainReviewPrTerminalsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the shared `_drain_review_pr_terminals` helper.

    `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict`
    all delegate their terminal arcs (merged PR -> `done`, closed PR ->
    `rejected`, open PR + manually-closed issue -> `rejected` without
    branch cleanup) to this helper. The per-stage handler tests cover the
    integrated behavior; these focused tests pin the helper contract
    (return value, event shape, branch-cleanup semantics, pr=None no-op)
    independently of any stage wiring.
    """

    def _state_with_pr_number(self, gh, issue_number, pr_number, **extra):
        from orchestrator.github import PinnedState

        seed = {"pr_number": pr_number, **extra}
        gh.seed_state(issue_number, **seed)
        return PinnedState(comment_id=None, data=dict(seed))

    def test_pr_none_returns_false_no_op(self) -> None:
        # Fixing's PR-fetch failure path sets `pr=None` and hands it
        # straight to the helper; the helper must treat that as a no-op
        # so the calling handler can fall through to its own fetch-
        # failure deferral (the `if pr is None: return` guard further
        # down the fixing body). No label change, no state writes, no
        # cleanup, no events.
        gh = FakeGitHubClient()
        issue = make_issue(310, label="fixing")
        gh.add_issue(issue)
        state = self._state_with_pr_number(gh, 310, 31000)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, None, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_open_pr_open_issue_returns_false(self) -> None:
        # The handler-side rescan / debounce / drift logic depends on
        # the helper returning False for a "nothing terminal" state so
        # the caller can continue with the same `pr`.
        gh = FakeGitHubClient()
        issue = make_issue(311, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31100, head_branch="orchestrator/issue-311",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 311, 31100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_merged_pr_finalizes_to_done_with_event_and_cleanup(self) -> None:
        # The merged arc: stamp `merged_at`, flip to `done`, emit
        # `pr_merged` with `merge_method="external"` and the supplied
        # stage, close the issue if still open, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(312, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=31200, head_branch="orchestrator/issue-312",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 312, 31200, review_round=2, conflict_round=0,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((312, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 312,
        )
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "fixing")
        self.assertEqual(ev["pr_number"], 31200)
        self.assertEqual(ev["merge_method"], "external")
        self.assertEqual(ev["sha"], "cafe1234")
        self.assertEqual(ev["review_round"], 2)

    def test_closed_unmerged_pr_finalizes_to_rejected_with_event_and_cleanup(
        self,
    ) -> None:
        # The closed-PR arc: stamp `closed_without_merge_at`, flip to
        # `rejected`, emit `pr_closed_without_merge` with the supplied
        # stage, close the issue if still open, and run branch cleanup.
        # The branch is dead weight once the PR is gone, mirroring the
        # merged-PR cleanup order.
        gh = FakeGitHubClient()
        issue = make_issue(313, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31300, head_branch="orchestrator/issue-313",
            head=FakePRRef(sha="dead0001"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 313, 31300, review_round=3, conflict_round=2,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((313, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 313,
        )
        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev = closed_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        self.assertEqual(ev["pr_number"], 31300)
        self.assertEqual(ev["sha"], "dead0001")
        self.assertEqual(ev["review_round"], 3)
        self.assertEqual(ev["conflict_round"], 2)

    def test_open_pr_with_manually_closed_issue_rejects_without_cleanup(
        self,
    ) -> None:
        # Open PR + manually closed issue is a human stop signal: flip
        # to `rejected` so the in_review HITL ready-ping cannot
        # advertise the PR as ready for human merge over the human
        # rejection, but deliberately leave the branch alone so the
        # operator can salvage / reopen the still-open PR. No event
        # emit either -- `pr_closed_without_merge` is reserved for the
        # genuine closed-PR arc above.
        gh = FakeGitHubClient()
        issue = make_issue(314, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31400, head_branch="orchestrator/issue-314",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 314, 31400)

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((314, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        # The PR is still open and may be reopened / salvaged, so the
        # branch must survive this exit.
        result["_cleanup_terminal_branch"].assert_not_called()
        # No `pr_closed_without_merge` emit for the open-PR case.
        self.assertEqual(
            [e for e in gh.recorded_events
             if e["event"] == "pr_closed_without_merge"],
            [],
        )
        self.assertEqual(
            [e for e in gh.recorded_events if e["event"] == "pr_merged"],
            [],
        )

    def test_resolving_conflict_terminal_preserves_zero_conflict_round(
        self,
    ) -> None:
        # Legacy / manually-relabelled `resolving_conflict` states may
        # land in the terminal arcs without `conflict_round` ever being
        # seeded (the in_review route normally initializes it to 0
        # before flipping the label). The pre-refactor inline code
        # coerced the value via `int(state.get("conflict_round") or 0)`
        # so the audit record always carried the field. `build_event_record`
        # drops None-valued extras, so the helper must keep that coercion
        # for `stage="resolving_conflict"` -- otherwise legacy states
        # silently lose `conflict_round` from `pr_merged` /
        # `pr_closed_without_merge` events.
        gh = FakeGitHubClient()
        issue = make_issue(316, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31600, head_branch="orchestrator/issue-316",
            head=FakePRRef(sha="feed1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        # Deliberately omit `conflict_round` from the pinned state.
        state = self._state_with_pr_number(gh, 316, 31600)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        # Field must be present (build_event_record drops None), and
        # the coerced default must be 0.
        self.assertIn("conflict_round", ev)
        self.assertEqual(ev["conflict_round"], 0)

        # Same coercion for the closed-without-merge arc.
        issue2 = make_issue(317, label="resolving_conflict")
        gh.add_issue(issue2)
        pr2 = FakePR(
            number=31700, head_branch="orchestrator/issue-317",
            head=FakePRRef(sha="feed5678"),
            merged=False, state="closed",
        )
        gh.add_pr(pr2)
        state2 = self._state_with_pr_number(gh, 317, 31700)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue2, state2, pr2,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev2 = closed_events[0]
        self.assertIn("conflict_round", ev2)
        self.assertEqual(ev2["conflict_round"], 0)

    def test_in_review_terminal_omits_missing_conflict_round(self) -> None:
        # The other two stages have always passed the raw
        # `state.get("conflict_round")` through, so a missing counter
        # naturally drops out via `build_event_record`. Pin that contract
        # so a future refactor doesn't accidentally start coercing for
        # `in_review` / `fixing` and start emitting a `conflict_round=0`
        # field on states that never had the counter.
        gh = FakeGitHubClient()
        issue = make_issue(318, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31800, head_branch="orchestrator/issue-318",
            head=FakePRRef(sha="cafe5678"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 318, 31800)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertNotIn("conflict_round", merged_events[0])

    def test_merged_arc_handles_already_closed_issue_without_re_closing(
        self,
    ) -> None:
        # A `Resolves #N` footer auto-closes the issue the moment the PR
        # merges, so when the closed-issue sweep yields this case the
        # helper sees an already-closed issue. The merged arc still
        # finalizes the label, but must not crash trying to re-close
        # what GitHub already closed.
        gh = FakeGitHubClient()
        issue = make_issue(315, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31500, head_branch="orchestrator/issue-315",
            head=FakePRRef(sha="feed0001"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 315, 31500)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((315, "done"), gh.label_history)
        self.assertTrue(issue.closed)
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0]["stage"], "fixing")




if __name__ == "__main__":
    unittest.main()
