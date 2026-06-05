# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import base_sync, config, workflow
from orchestrator.github import BACKLOG_LABEL, BASE_SYNC_HOLD_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakeUser,
    make_issue,
)


class RefreshBaseAndWorktreesUnitTest(unittest.TestCase):
    """Unit-level coverage for the per-tick base refresh helper. Real-git
    integration coverage lives in `RefreshBaseAndWorktreesRealGitTest`
    (`tests/test_workflow_base_sync_real_git.py`).
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-unit-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.target_root = self.tmpdir / "target"
        self.target_root.mkdir()
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self.target_root,
            base_branch="main",
        )
        self.gh = FakeGitHubClient()

    def test_returns_early_when_base_fetch_fails(self) -> None:
        from unittest.mock import MagicMock
        fetch_fail = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=1, stdout="", stderr="boom",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_fail), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_returns_early_when_repo_worktrees_root_missing(self) -> None:
        from unittest.mock import MagicMock
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_iterates_only_issue_dirs(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        # Two valid issue worktrees, one decompose dir (skipped), one stray
        # file (skipped), one malformed (skipped).
        (wt_root / "issue-7").mkdir()
        (wt_root / "issue-42").mkdir()
        (wt_root / "decompose-7").mkdir()
        (wt_root / "issue-bogus").mkdir()
        (wt_root / "stray.txt").write_text("x")

        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        called_numbers = sorted(c.args[3] for c in sync.call_args_list)
        self.assertEqual(called_numbers, [7, 42])

    def test_per_worktree_exception_is_swallowed(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        (wt_root / "issue-1").mkdir()
        (wt_root / "issue-2").mkdir()
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock(side_effect=[RuntimeError("kaboom"), None])
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # Both worktrees attempted despite the first raising.
        self.assertEqual(sync.call_count, 2)

    def test_base_fetch_uses_per_spec_authed_helper(self) -> None:
        # The base refresh must go through `_authed_target_fetch` (which
        # resolves the per-spec token and uses the spec's `remote_name`
        # for refs/remotes/<remote_name>/<branch>), NOT plain
        # `_git("fetch", ...)`. Without this, a multi-remote spec where
        # `remote_name != origin` falls back to the ambient git
        # credential helper -- which fails under systemd with
        # `terminal prompts disabled`.
        private_spec = config.RepoSpec(
            slug="acme/widget-private",
            target_root=self.target_root,
            base_branch="cache-main",
            remote_name="private",
        )
        fetch_calls: list[tuple] = []

        def fake_fetch(spec, branch):
            fetch_calls.append((spec, branch))
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        # Block any plain-git fetch to assert it never runs.
        plain_git_calls: list[tuple] = []

        def fake_git(*args, cwd):
            plain_git_calls.append(args)
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        with patch.object(base_sync, "_authed_target_fetch", side_effect=fake_fetch), \
             patch.object(base_sync, "_git", side_effect=fake_git), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ):
            workflow._refresh_base_and_worktrees(self.gh, private_spec)

        self.assertEqual(
            fetch_calls, [(private_spec, "cache-main")],
            "base refresh must route through `_authed_target_fetch` with "
            "the spec's base branch",
        )
        # No plain-git fetch was issued -- otherwise the multi-remote
        # token-selection regression resurfaces.
        for args in plain_git_calls:
            self.assertNotEqual(
                args[0] if args else "", "fetch",
                f"plain `_git(\"fetch\", ...)` leaked: {args!r}",
            )


class SyncWorktreeWithBaseUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt")
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="implementing"))

    def _git_result(self, *, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
        )

    def _hardened_for_recovery(self, remote_pr_head_sha: str):
        """`_git_hardened` side_effect that answers
        `rev-parse refs/remotes/<remote>/<branch>` with `remote_pr_head_sha`
        and returns a default success for every other call.

        `_recover_pending_auto_base_rebase` now reads the freshly-fetched
        remote PR head via `git rev-parse refs/remotes/...` instead of
        relying on `_branch_ahead_behind`'s `(0, 0)`-on-error return as
        proof that local HEAD == remote PR head. Recovery tests therefore
        need to feed the recovery the SHA they want it to see.
        """
        def side_effect(*args, **kwargs):
            if (
                len(args) >= 2
                and args[0] == "rev-parse"
                and isinstance(args[1], str)
                and args[1].startswith("refs/remotes/")
            ):
                return self._git_result(stdout=f"{remote_pr_head_sha}\n")
            return self._git_result()
        return side_effect

    def _add_pr(
        self,
        *,
        pr_number: int = 42,
        head_branch: str = "orchestrator/acme__widget/issue-7",
        merged: bool = False,
        state: str = "open",
    ) -> FakePR:
        pr = FakePR(
            number=pr_number, head_branch=head_branch,
            merged=merged, state=state,
        )
        self.gh.add_pr(pr)
        return pr

    def test_pr_having_in_review_clean_rebase_routes_to_validating(
        self,
    ) -> None:
        # A clean base rebase on an open PR branch must NOT be relabeled
        # to `resolving_conflict` -- that label is reserved for actual
        # content conflicts (or an operator manual application). Instead
        # the refresh rebases locally, pushes with force-with-lease pinned
        # to the pre-rebase SHA, resets `review_round`, and hands the
        # issue back to `validating` so the reviewer re-runs against the
        # rewritten head.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7", review_round=3,
        )
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        # Behind base by 3 commits.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Rebase ran exactly once on the worktree.
        merge.assert_called_once()
        # Push with force-with-lease pinned to the pre-rebase SHA.
        push.assert_called_once()
        push_kwargs = push.call_args.kwargs
        self.assertEqual(push_kwargs.get("force_with_lease"), "before-sha")
        # Label flipped to validating, NOT resolving_conflict.
        self.assertIn((7, "validating"), self.gh.label_history)
        self.assertNotIn((7, "resolving_conflict"), self.gh.label_history)
        # PR comment names validating (not resolving_conflict).
        self.assertEqual(len(self.gh.posted_pr_comments), 1)
        self.assertEqual(self.gh.posted_pr_comments[0][0], 42)
        self.assertIn("validating", self.gh.posted_pr_comments[0][1])
        # `review_round` was reset to 0 so the reviewer re-runs.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("review_round"), 0)
        # `conflict_round` was NOT seeded -- this is no longer a conflict path.
        self.assertIsNone(data.get("conflict_round"))
        # A `base_rebased` audit event was emitted carrying the new head SHA.
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 1)
        self.assertEqual(rebased[0].get("sha"), "after-sha")
        self.assertEqual(rebased[0].get("stage"), "in_review")
        # No `conflict_round` audit event for a clean rebase.
        conflict_rounds = [
            e for e in self.gh.recorded_events if e.get("event") == "conflict_round"
        ]
        self.assertEqual(conflict_rounds, [])

    def test_pr_having_conflicting_rebase_routes_to_resolving_conflict(
        self,
    ) -> None:
        # When the local base rebase leaves conflicted files, the refresh
        # DOES relabel to `resolving_conflict` so the handler can drive
        # the dev agent. This is the only path that still enters
        # `resolving_conflict` from the refresh.
        from unittest.mock import MagicMock
        from tests.fakes import FakePR, FakePRRef
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        # Seed an explicit PR head SHA on the PR so the
        # `conflict_round` `action="entered"` event can be asserted
        # against it (the event historically carried the remote PR
        # head SHA and `docs/observability.md` still documents `sha`
        # as part of the event shape).
        self.gh.add_pr(FakePR(
            number=42, head_branch="orchestrator/acme__widget/issue-7",
            head=FakePRRef(sha="cafef00dcafef00d"),
            state="open",
        ))
        merge = MagicMock(return_value=(False, ["src/feature.py", "tests/foo.py"]))
        push = MagicMock()
        head_sha = MagicMock(return_value="before-sha")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Rebase was attempted, then aborted.
        merge.assert_called_once()
        abort_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("rebase", "--abort")
        ]
        self.assertEqual(len(abort_calls), 1)
        # Push MUST NOT have been called -- the agent resolves the conflicts.
        push.assert_not_called()
        # Label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        self.assertNotIn((7, "validating"), self.gh.label_history)
        # PR comment names the conflict count.
        self.assertEqual(len(self.gh.posted_pr_comments), 1)
        self.assertIn("conflicted file(s)", self.gh.posted_pr_comments[0][1])
        # `conflict_round` initialized to 0 (the cap counter).
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 0)
        # A `conflict_round` "entered" audit event was emitted and
        # carries the remote PR head SHA in its `sha` field. The
        # field is preserved here because consumers of the event log
        # (`docs/observability.md`) document it as part of the event
        # shape, and previous orchestrator versions populated it
        # too -- a null sha here would silently break event-log
        # downstreams that key off the field.
        entered = [
            e for e in self.gh.recorded_events
            if e.get("event") == "conflict_round" and e.get("action") == "entered"
        ]
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0].get("stage"), "in_review")
        self.assertEqual(entered[0].get("sha"), "cafef00dcafef00d")

    def test_pr_having_validating_clean_rebase_routes_to_validating(self) -> None:
        # Validating + clean rebase: stays validating (label flip is a
        # no-op semantically but is still emitted so the reviewer
        # restarts on the new head). The test asserts the resolving_conflict
        # label is NOT applied.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="validating"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before", "after"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertIn((7, "validating"), self.gh.label_history)
        self.assertNotIn((7, "resolving_conflict"), self.gh.label_history)
        push.assert_called_once()

    def test_pr_having_documenting_clean_rebase_routes_to_validating(self) -> None:
        # `documenting` is the brief final-docs hop between reviewer
        # approval and `in_review`. The handler only checks ahead/behind
        # against the PR branch, not the base, so a sibling-PR merge
        # during the docs pass must be caught by the pre-tick refresh --
        # otherwise the docs commit would land on a stale base. The new
        # clean-rebase path lands on `validating` (the docs pass re-runs
        # only after the next reviewer approval), so the issue does NOT
        # get relabeled to `resolving_conflict`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="documenting"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before", "after"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Clean rebase -> validating; resolving_conflict is reserved for
        # actual conflicted files.
        self.assertIn((7, "validating"), self.gh.label_history)
        self.assertNotIn((7, "resolving_conflict"), self.gh.label_history)

    def test_pr_clean_rebase_push_failure_resets_and_parks(self) -> None:
        # Force-with-lease rejection (diverged branch / crash recovery)
        # or any other push failure must NOT flip the label, must
        # reset local HEAD back to the pre-rebase SHA so the worktree
        # stays in sync with the remote PR head, AND must park the
        # issue awaiting human attention so the in_review / fixing /
        # validating / documenting handler that `tick()` dispatches
        # right after the refresh short-circuits on `awaiting_human`
        # instead of processing the issue on a behind-base PR head.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=False)  # lease rejection
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Label stays put: the operator may want to inspect why the
        # lease rejected before any relabel happens.
        self.assertEqual(self.gh.label_history, [])
        # review_round was NOT reset since we did not flip the label.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("review_round"))
        # Critical: local HEAD was reset back to the pre-rebase SHA so
        # the next tick's behind check still reports behind > 0 and
        # the validating reviewer / in_review handler do not read a
        # local HEAD that is NOT on the PR.
        reset_calls = [
            c for c in hardened.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(
            len(reset_calls), 1,
            f"expected exactly one `git reset --hard before-sha` after the "
            f"push failure, got {hardened.call_args_list!r}",
        )
        # Parked awaiting human with a stable, custom park_reason so
        # `_handle_validating`'s transient-park recovery branch does
        # NOT auto-clear the park (it only clears `push_failed` /
        # `agent_timeout` / `reviewer_timeout` / `reviewer_failed`).
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        # HITL message posted on the issue thread (NOT the PR thread):
        # `_park_awaiting_human` writes to the issue, which is where
        # the resume-on-human-reply comment scan reads from.
        self.assertEqual(len(self.gh.posted_comments), 1)
        body = self.gh.posted_comments[0][1]
        self.assertIn("force-with-lease", body)
        # The auto-rebase rejection path does NOT post on the PR thread
        # too -- that would spam every diverged PR with duplicate notices.
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_clean_rebase_push_failure_skips_subsequent_handler(self) -> None:
        # Tick-level regression for the same #413 review case: after
        # the refresh parks the issue on a push failure, the
        # in_review handler that `_process_issue` runs in the same
        # tick must observe `awaiting_human=True` and short-circuit
        # without spawning the reviewer, posting on the PR, or firing
        # the HITL ready-ping for the behind-base PR head.
        from unittest.mock import MagicMock

        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()

        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=False)
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())

        # Spy on `_handle_in_review` so we can assert it ran exactly
        # once and observed `awaiting_human=True` (the short-circuit
        # path) without spawning the reviewer or posting on the PR.
        in_review_calls: list[bool] = []

        def fake_in_review(gh, spec, issue):
            data = gh.pinned_data(issue.number)
            in_review_calls.append(bool(data.get("awaiting_human")))

        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened), \
             patch.object(workflow, "_handle_in_review", side_effect=fake_in_review):
            # The refresh path parks the issue; the dispatcher then
            # runs `_handle_in_review` against the now-parked state.
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
            workflow._process_issue(self.gh, self.spec, self.gh._issues[7])

        self.assertEqual(
            in_review_calls, [True],
            "in_review handler must run exactly once and observe "
            "`awaiting_human=True` (the awaiting-human gate at handler "
            "entry then short-circuits the issue this tick)",
        )
        # No PR notice posted on this tick -- the HITL ping that would
        # otherwise advertise the behind-base PR is suppressed by the
        # awaiting_human park.
        self.assertEqual(self.gh.posted_pr_comments, [])
        # No relabel to `validating` / `resolving_conflict` since the
        # rebase did not land on the PR.
        self.assertEqual(self.gh.label_history, [])

    def test_pr_clean_rebase_dirty_after_rebase_resets_and_parks(self) -> None:
        # A pre-existing uncommitted edit that survives the rebase must
        # NOT be force-pushed alongside the rebase result -- the validating
        # reviewer would otherwise vote on a tree that does not match the
        # PR head. The refresh resets local HEAD back to the pre-rebase
        # SHA (so the same-tick handler dispatch does not see a rebased
        # local HEAD that is NOT on the PR), runs `git clean -fd` to
        # discard untracked leftovers, and parks awaiting human with
        # `park_reason="auto_base_rebase_dirty"`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock()
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())

        # First dirty-check (the pre-rebase pre-flight) clean; second
        # dirty-check (after the rebase, just before push) dirty.
        dirty_calls = iter([[], ["scratch.py"]])
        with patch.object(
            base_sync, "_worktree_dirty_files",
            side_effect=lambda *_a, **_k: next(dirty_calls),
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        push.assert_not_called()
        # No label flip; the issue keeps its current label until the
        # operator resolves the dirty tree.
        self.assertEqual(self.gh.label_history, [])
        # Reset local HEAD back to the pre-rebase SHA and clean up
        # untracked files left by the rebase.
        reset_calls = [
            c for c in hardened.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(len(reset_calls), 1, hardened.call_args_list)
        clean_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("clean", "-fd")
        ]
        self.assertEqual(len(clean_calls), 1, hardened.call_args_list)
        # Parked awaiting human with the auto-rebase dirty reason.
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "auto_base_rebase_dirty")
        # HITL message landed on the issue thread.
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertIn("uncommitted change", self.gh.posted_comments[0][1])

    def test_pr_rebase_failed_without_conflicts_parks(self) -> None:
        # A rebase failure that produces no conflicted files (planted
        # hook, smudge filter, permissions, ...) restores the worktree
        # to the pre-rebase SHA via `git rebase --abort` and then parks
        # awaiting human with `park_reason="auto_base_rebase_failed"`.
        # Without this park the same-tick handler dispatch would let
        # in_review / fixing / validating / documenting continue on a
        # behind-base PR.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(False, []))
        push = MagicMock()
        head_sha = MagicMock(return_value="before-sha")
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # `git rebase --abort` issued exactly once.
        abort_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("rebase", "--abort")
        ]
        self.assertEqual(len(abort_calls), 1, hardened.call_args_list)
        # No push, no label flip.
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        # Parked awaiting human with the rebase-failed reason.
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "auto_base_rebase_failed")
        # HITL message on the issue thread.
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertIn("non-conflict reason", self.gh.posted_comments[0][1])

    def test_pr_auto_rebase_park_recovers_on_new_human_comment(self) -> None:
        # Recovery path: an issue parked on an auto-rebase park reason
        # (push/dirty/failed) gets its park cleared by a new human
        # comment on the issue thread, and the refresh re-attempts the
        # rebase + push on that same tick. Without this branch the
        # park would be permanent because no stage handler knows how
        # to drive an auto-rebase retry.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True,
            park_reason="auto_base_rebase_push_failed",
            last_action_comment_id=99,
        )
        self._add_pr()
        # Fresh human comment landed after the park's watermark.
        self.gh._issues[7].comments.append(FakeComment(
            id=200, body="branch reconciled, please retry",
            user=FakeUser("human"),
        ))
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Recovery cleared the park flags and the watermark advanced
        # past the consumed human comment.
        data = self.gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("last_action_comment_id"), 200)
        # Clean rebase + push succeeded; issue routed to validating.
        merge.assert_called_once()
        push.assert_called_once()
        self.assertIn((7, "validating"), self.gh.label_history)
        # `review_round` reset for the reviewer's next pass.
        self.assertEqual(data.get("review_round"), 0)

    def test_pr_auto_rebase_park_survives_early_exit_when_dirty(self) -> None:
        # Regression: the awaiting_human-clear + watermark-advance for
        # an auto-rebase-park retry must NOT land on disk until the
        # rebase is actually committed. Before this fix, the refresh
        # cleared the park up front; if a later gate (dirty check,
        # PR fetch failure, hold_base_sync) early-returned, the issue
        # was left unparked + watermark-advanced even though no retry
        # happened, so the same-tick stage handlers could run on the
        # still-behind PR head and consume the operator's "retry"
        # comment as fresh feedback.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True,
            park_reason="auto_base_rebase_push_failed",
            last_action_comment_id=99,
        )
        self._add_pr()
        # Fresh human comment past the watermark.
        self.gh._issues[7].comments.append(FakeComment(
            id=200, body="reconciled, please retry",
            user=FakeUser("human"),
        ))
        # The pre-rebase dirty check fires (worktree has uncommitted
        # changes left by some external race after the prior park).
        merge = MagicMock()
        push = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=["scratch.py"],
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No rebase / no push / no relabel.
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        # CRITICAL: park survives on disk, watermark NOT advanced.
        # The operator's "retry" comment is still ahead of the
        # watermark so the next refresh tick rediscovers it.
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        self.assertEqual(data.get("last_action_comment_id"), 99)

    def test_pr_auto_rebase_park_survives_early_exit_when_pr_fetch_fails(
        self,
    ) -> None:
        # Same regression for the `gh.get_pr()` failure gate: a
        # transient PR fetch failure must leave the park on disk so
        # the same-tick handlers do not run on the still-behind PR.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True,
            park_reason="auto_base_rebase_push_failed",
            last_action_comment_id=99,
        )
        # No PR added -- `gh.get_pr` raises.
        self.gh._issues[7].comments.append(FakeComment(
            id=200, body="retry", user=FakeUser("human"),
        ))
        merge = MagicMock()
        push = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        self.assertEqual(data.get("last_action_comment_id"), 99)

    def test_pr_auto_rebase_park_survives_early_exit_when_hold_added(
        self,
    ) -> None:
        # Same regression for the `hold_base_sync` gate: an operator
        # who applies `hold_base_sync` AFTER replying to the park
        # message has explicitly paused auto-rebase. The park must
        # survive on disk so handlers do not run unprotected.
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True,
            park_reason="auto_base_rebase_push_failed",
            last_action_comment_id=99,
        )
        self._add_pr()
        self.gh._issues[7].comments.append(FakeComment(
            id=200, body="retry", user=FakeUser("human"),
        ))
        merge = MagicMock()
        push = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        self.assertEqual(data.get("last_action_comment_id"), 99)

    def test_pr_auto_rebase_park_stays_parked_without_new_comment(self) -> None:
        # No new human comment after the park's watermark -- the human
        # has not acknowledged the failure yet, so the issue stays
        # parked. No rebase attempt, no relabel.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True,
            park_reason="auto_base_rebase_push_failed",
            last_action_comment_id=99,
        )
        self._add_pr()
        # No new comments past the watermark.
        merge = MagicMock()
        push = MagicMock()
        head_sha = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No rebase, no push, no relabel; park still in place.
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )

    def test_pr_non_auto_rebase_park_still_skips(self) -> None:
        # A non-auto-rebase park (e.g. `unmergeable` from
        # `_handle_in_review`'s analog) must NOT be cleared by the
        # refresh, even when there is a new human comment -- the stage
        # handler owns those parks. Mirrors the existing
        # `test_pr_route_skips_when_awaiting_human` regression but
        # with a fresh human comment so the recovery branch can't
        # accidentally take it.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True, park_reason="unmergeable",
            last_action_comment_id=99,
        )
        self.gh._issues[7].comments.append(FakeComment(
            id=200, body="ack", user=FakeUser("human"),
        ))
        self._add_pr()
        merge = MagicMock()
        push = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No rebase, no relabel, watermark untouched.
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")
        self.assertEqual(data.get("last_action_comment_id"), 99)

    def test_pr_crash_recovery_pushes_unpushed_rebase(self) -> None:
        # Scenario 1: a prior tick set `pending_auto_base_rebase_push_sha`
        # to the pre-rebase SHA, ran `_rebase_base_into_worktree`
        # successfully (HEAD moved to the rebased SHA), but the
        # orchestrator died before `_push_branch` ran. Local HEAD is
        # ahead of the remote PR head; the next refresh tick must
        # detect the pending flag, push the recovered rebase with the
        # original lease, and relabel to `validating`. Without this
        # recovery the next tick's `behind == 0` check would skip the
        # worktree and validating would review a SHA not on the PR.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
        )
        self._add_pr()
        # `behind == 0` since the rebase already replayed the base
        # advance onto local HEAD.
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        merge = MagicMock()
        # Local HEAD is the rebased SHA; differs from the stored
        # pre-rebase anchor so the recovery branch doesn't bail as a
        # stale flag.
        head_sha = MagicMock(return_value="rebased-sha")
        # `_branch_ahead_behind` reports ahead-of-remote-PR-head
        # (= push pending).
        ahead_behind = MagicMock(return_value=(1, 0))
        # `_authed_fetch` succeeds.
        fetch = MagicMock(return_value=self._git_result())
        push = MagicMock(return_value=True)
        # Remote PR head is still at the pre-rebase SHA -- the prior
        # tick's rebase moved local HEAD but the push never landed.
        hardened = MagicMock(side_effect=self._hardened_for_recovery("before-sha"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Recovery pushed with `force_with_lease` pinned to the
        # original pre-rebase SHA.
        push.assert_called_once()
        self.assertEqual(
            push.call_args.kwargs.get("force_with_lease"), "before-sha",
        )
        # Normal rebase did NOT run again -- recovery handled it.
        merge.assert_not_called()
        # Label flipped to `validating`, anchor cleared, review_round
        # reset.
        self.assertIn((7, "validating"), self.gh.label_history)
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertEqual(data.get("review_round"), 0)
        # `base_rebased` audit event records the crash-recovery method.
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 1)
        self.assertEqual(rebased[0].get("method"), "crash_recovery_pushed")
        self.assertEqual(rebased[0].get("sha"), "rebased-sha")

    def test_pr_crash_recovery_finalizes_when_push_already_landed(self) -> None:
        # Scenario 2: a prior tick set the anchor, rebased, AND pushed
        # successfully, but died before the post-push relabel +
        # `review_round=0` write. Local HEAD == remote PR head (both
        # at the rebased SHA); the next refresh tick must finalize the
        # relabel without issuing a duplicate push. Without this
        # recovery `in_review` / `documenting` / `fixing` would
        # continue on stale label + `review_round` state after a
        # branch rewrite.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
            review_round=3,
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        merge = MagicMock()
        head_sha = MagicMock(return_value="rebased-sha")
        # Local HEAD == remote PR head: ahead == 0, behind == 0.
        ahead_behind = MagicMock(return_value=(0, 0))
        fetch = MagicMock(return_value=self._git_result())
        push = MagicMock()
        # Remote PR head is at the rebased SHA -- the prior tick's
        # push landed; the recovery must read the same SHA via
        # `rev-parse` to confirm the in-sync state instead of trusting
        # `_branch_ahead_behind`'s ambiguous (0, 0).
        hardened = MagicMock(side_effect=self._hardened_for_recovery("rebased-sha"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # NO push reissued -- the previous tick's push already landed.
        push.assert_not_called()
        merge.assert_not_called()
        # Label finalized to `validating`, anchor cleared, review_round
        # reset (was 3, now 0).
        self.assertIn((7, "validating"), self.gh.label_history)
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertEqual(data.get("review_round"), 0)
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 1)
        self.assertEqual(
            rebased[0].get("method"), "crash_recovery_relabel_only",
        )

    def test_pr_crash_recovery_clears_stale_flag_when_head_unchanged(
        self,
    ) -> None:
        # Scenario 3: a prior tick set the anchor, then died before
        # `_rebase_base_into_worktree` could move HEAD (or the rebase
        # was reverted). Local HEAD == stored anchor SHA. The recovery
        # branch clears the flag and falls through to the normal
        # behind-base flow, which then attempts the rebase fresh.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
        )
        self._add_pr()
        # Behind base by 2 (the original behind that triggered the
        # rebase before the crash).
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        # First `_head_sha` (recovery) reports the pre-rebase SHA
        # (matches the anchor); subsequent calls (normal flow) also
        # return the same value until the new rebase moves HEAD.
        head_sha = MagicMock(side_effect=[
            "before-sha", "before-sha", "after-sha",
        ])
        # Normal flow: clean rebase + successful push.
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        fetch = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # The normal rebase ran (recovery did NOT short-circuit it).
        merge.assert_called_once()
        push.assert_called_once()
        # Anchor cleared and label flipped to `validating`.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertIn((7, "validating"), self.gh.label_history)
        # Normal-flow rebase event, NOT a crash-recovery one.
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 1)
        self.assertEqual(rebased[0].get("method"), "auto_clean_rebase")

    def _run_unverifiable_recovery(
        self,
        *,
        fetch_returncode: int = 0,
        rev_parse_returncode: int = 0,
        rev_parse_stdout: str = "remote-sha\n",
        ahead_behind: tuple = (1, 0),
        local_head: str = "rebased-sha",
    ):
        """Helper for the four `_recover_pending_auto_base_rebase`
        cannot-verify regressions: seed a flag-pinned in_review issue,
        wire mocks per arguments, and run the refresh once.

        Returns a `(hardened_mock, push_mock, merge_mock)` triple so the
        caller can assert on the reset call and the no-push / no-rebase
        invariant.
        """
        from unittest.mock import MagicMock

        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
            review_round=3,
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        head_sha_mock = MagicMock(return_value=local_head)
        ahead_behind_mock = MagicMock(return_value=ahead_behind)
        fetch_mock = MagicMock(
            return_value=self._git_result(returncode=fetch_returncode),
        )
        push_mock = MagicMock()
        merge_mock = MagicMock()

        def fake_hardened(*args, **kwargs):
            if (
                len(args) >= 2
                and args[0] == "rev-parse"
                and isinstance(args[1], str)
                and args[1].startswith("refs/remotes/")
            ):
                return self._git_result(
                    returncode=rev_parse_returncode,
                    stdout=rev_parse_stdout,
                )
            return self._git_result()

        hardened_mock = MagicMock(side_effect=fake_hardened)
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge_mock), \
             patch.object(base_sync, "_head_sha", head_sha_mock), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind_mock), \
             patch.object(base_sync, "_authed_fetch", fetch_mock), \
             patch.object(base_sync, "_push_branch", push_mock), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        return hardened_mock, push_mock, merge_mock

    def _assert_recovery_unverified_reset_and_park(
        self, hardened_mock, push_mock, merge_mock
    ) -> None:
        """Common assertions for the four cannot-verify recovery exits.

        Every such exit must (a) reset local HEAD to the pre-rebase
        anchor (so the worktree matches the last-known remote PR head
        and the same-tick handler dispatch cannot read a SHA the PR
        may not carry), (b) park awaiting human with
        `auto_base_rebase_push_failed` (so the dispatcher's
        `awaiting_human` short-circuit fires on every PR-stage
        handler this tick), and (c) clear the anchor (the reset put
        HEAD back at it, so a follow-up tick would hit case 1
        anyway).
        """
        # Reset to the pre-rebase SHA was issued.
        reset_calls = [
            c for c in hardened_mock.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(len(reset_calls), 1, hardened_mock.call_args_list)
        # No push, no merge, no relabel -- recovery aborted before
        # finalize.
        push_mock.assert_not_called()
        merge_mock.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        # Anchor cleared (reset put HEAD back at it).
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        # Same-tick handler dispatch will short-circuit on this park.
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        # No `base_rebased` event -- we did NOT route to validating.
        rebased = [
            e for e in self.gh.recorded_events
            if e.get("event") == "base_rebased"
        ]
        self.assertEqual(rebased, [])

    def test_pr_crash_recovery_aborts_with_park_on_fetch_failure(
        self,
    ) -> None:
        # Regression: the `_authed_fetch` failure path used to
        # `return True` without parking, letting the same-tick
        # validating / in_review / fixing / documenting handler run
        # against a local SHA that recovery had NOT verified is on
        # the PR. The fix resets HEAD to the pre-rebase anchor and
        # parks awaiting human so handler dispatch short-circuits.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            fetch_returncode=128,
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock, push_mock, merge_mock,
        )

    def test_pr_crash_recovery_aborts_with_park_on_rev_parse_failure(
        self,
    ) -> None:
        # Same regression for the `rev-parse` failure path. Without
        # the park, `validating` could read the rebased local HEAD
        # (which may not be on the PR) and stamp its review against
        # a SHA the human-merge gate cannot match.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_returncode=128, rev_parse_stdout="",
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock, push_mock, merge_mock,
        )

    def test_pr_crash_recovery_aborts_with_park_on_empty_remote_sha(
        self,
    ) -> None:
        # `rev-parse` returncode 0 but empty stdout -- same threat
        # model, same fix.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_stdout="\n",
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock, push_mock, merge_mock,
        )

    def test_pr_crash_recovery_aborts_with_park_on_sha_mismatch_with_zero_ahead_behind(
        self,
    ) -> None:
        # The fourth cannot-verify path: rev-parse returns a DIFFERENT
        # SHA than local HEAD AND `_branch_ahead_behind` returns
        # `(0, 0)` (which now necessarily means a stale remote-
        # tracking ref since the SHA inequality has ruled out the
        # legitimate in-sync case). Reset + park, same as the other
        # three.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_stdout="foreign-sha\n",
            ahead_behind=(0, 0),
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock, push_mock, merge_mock,
        )

    def test_pr_crash_recovery_case_2_with_behind_falls_through_to_normal_rebase(
        self,
    ) -> None:
        # Regression: case 2 (HEAD == remote PR head; push landed on
        # prior tick) finalized straight to `validating` regardless of
        # whether base advanced AGAIN since the interrupted rebase. If
        # the freshly fetched base is ahead of the recovered head,
        # routing to `validating` lets the same-tick reviewer run on a
        # PR that is still behind base. The fix passes `behind` to
        # the recovery helper: case 2 still posts the recovery PR
        # notice + emits `base_rebased(method="crash_recovery_relabel_only")`
        # + resets `review_round` + clears the anchor, but does NOT
        # relabel when `behind > 0`. Instead, it returns False so the
        # caller's normal rebase + push flow rebases the recovered head
        # onto the newer base before routing to `validating`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
            review_round=3,
        )
        self._add_pr()
        # `behind == 2`: base advanced after the interrupted rebase.
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        # Sequence of `_head_sha` reads:
        #   1. recovery's read after the fetch -> "rebased-sha"
        #   2. normal flow's `before_sha = _head_sha(worktree)` -> "rebased-sha"
        #   3. normal flow's `after_sha = _head_sha(worktree)` -> "new-rebased-sha"
        head_sha = MagicMock(
            side_effect=["rebased-sha", "rebased-sha", "new-rebased-sha"],
        )
        # Remote PR head == local HEAD (recovery's case-2 SHA match).
        hardened = MagicMock(side_effect=self._hardened_for_recovery("rebased-sha"))
        fetch = MagicMock(return_value=self._git_result())
        # Normal flow's rebase succeeds without conflicts.
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Normal flow's rebase + push ran (recovery fell through).
        merge.assert_called_once()
        push.assert_called_once()
        # Lease is pinned to the recovery's confirmed remote SHA (=
        # the recovered head that is now the live PR head).
        self.assertEqual(
            push.call_args.kwargs.get("force_with_lease"), "rebased-sha",
        )
        # Final label is `validating`, anchor cleared, review_round
        # reset (was 3, now 0).
        self.assertIn((7, "validating"), self.gh.label_history)
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertEqual(data.get("review_round"), 0)
        # Two `base_rebased` events: one for the case-2 finalize-
        # without-relabel and one for the normal-flow rebase + push.
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 2)
        methods = [e.get("method") for e in rebased]
        self.assertEqual(
            methods, ["crash_recovery_relabel_only", "auto_clean_rebase"],
        )
        # Final head SHA on the audit trail is the freshly-rebased one.
        self.assertEqual(rebased[1].get("sha"), "new-rebased-sha")

    def test_pr_crash_recovery_case_3_with_behind_falls_through_to_normal_rebase(
        self,
    ) -> None:
        # Same regression for case 3 (HEAD ahead of remote PR head;
        # push pending). The recovery still pushes the recovered head
        # (so the remote PR branch catches up to the interrupted
        # rebase result) and emits
        # `base_rebased(method="crash_recovery_pushed")`, but with
        # `behind > 0` it falls through to the normal flow which then
        # rebases against the newer base and pushes once more.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
            review_round=3,
        )
        self._add_pr()
        # `behind == 2`: base advanced again since the interrupted
        # rebase.
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        # `_head_sha` reads in order:
        #   1. recovery's read after fetch -> "rebased-sha"
        #   2. normal flow's `before_sha` -> "rebased-sha" (recovery's push didn't move HEAD locally)
        #   3. normal flow's `after_sha` -> "new-rebased-sha"
        head_sha = MagicMock(
            side_effect=["rebased-sha", "rebased-sha", "new-rebased-sha"],
        )
        # Recovery sees HEAD ahead of remote (case 3).
        ahead_behind = MagicMock(return_value=(1, 0))
        # Remote PR head differs from local HEAD: "old-remote-sha".
        hardened = MagicMock(side_effect=self._hardened_for_recovery("old-remote-sha"))
        fetch = MagicMock(return_value=self._git_result())
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Push was called TWICE: once by recovery (lease against the
        # pre-rebase anchor) and once by the normal flow (lease against
        # the post-recovery local HEAD = the just-pushed recovered SHA).
        self.assertEqual(push.call_count, 2)
        leases = [c.kwargs.get("force_with_lease") for c in push.call_args_list]
        self.assertEqual(leases, ["before-sha", "rebased-sha"])
        # Rebase ran exactly once -- the recovery's case 3 does NOT
        # re-run the rebase locally (the recovered SHA already carries
        # the interrupted rebase result); only the normal flow does.
        merge.assert_called_once()
        # Final label is `validating`, anchor cleared.
        self.assertIn((7, "validating"), self.gh.label_history)
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertEqual(data.get("review_round"), 0)
        # Two `base_rebased` events: recovery + normal flow.
        rebased = [e for e in self.gh.recorded_events if e.get("event") == "base_rebased"]
        self.assertEqual(len(rebased), 2)
        methods = [e.get("method") for e in rebased]
        self.assertEqual(
            methods, ["crash_recovery_pushed", "auto_clean_rebase"],
        )
        self.assertEqual(rebased[1].get("sha"), "new-rebased-sha")

    def test_pr_crash_recovery_diverged_resets_and_parks(self) -> None:
        # Scenario 4: a prior tick set the anchor, rebased, and the
        # remote PR branch was updated out-of-band before the next
        # tick (local HEAD is ahead AND behind remote -- truly
        # diverged). Recovery must reset HEAD to the pre-rebase SHA
        # and park awaiting human; force-pushing the local recovered
        # rebase here would clobber the out-of-band update.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        head_sha = MagicMock(return_value="rebased-sha")
        # Truly diverged: ahead 1, behind 1.
        ahead_behind = MagicMock(return_value=(1, 1))
        fetch = MagicMock(return_value=self._git_result())
        push = MagicMock()
        merge = MagicMock()
        # Remote PR head differs from both the rebased SHA and the
        # pre-rebase anchor (someone else updated the branch out-of-
        # band). The recovery routes by `_branch_ahead_behind` after
        # confirming the SHA mismatch.
        hardened = MagicMock(side_effect=self._hardened_for_recovery("foreign-sha"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Reset to the pre-rebase SHA was issued.
        reset_calls = [
            c for c in hardened.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(len(reset_calls), 1, hardened.call_args_list)
        # No push, no rebase, no relabel -- park with the recovery's
        # push-failed reason, anchor cleared.
        push.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )

    def test_pr_normal_rebase_sets_then_clears_recovery_anchor(self) -> None:
        # Every normal-flow clean rebase must set
        # `pending_auto_base_rebase_push_sha` BEFORE the rebase
        # (provides a recovery signal if a crash lands between the
        # rebase and the push), then clear it as part of the final
        # success state write.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7", review_round=2,
        )
        self._add_pr()
        # Capture the pinned-state anchor value AT THE MOMENT the
        # rebase is invoked -- this is the recovery anchor that a
        # crash between the rebase call and the post-push state
        # write would leave behind on GitHub for the next tick to
        # find.
        anchor_seen_at_rebase: list[object] = []

        def fake_rebase(spec, worktree):
            anchor_seen_at_rebase.append(
                self.gh.pinned_data(7).get(
                    "pending_auto_base_rebase_push_sha"
                )
            )
            return (True, [])

        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(
                base_sync, "_rebase_base_into_worktree",
                side_effect=fake_rebase,
             ), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Anchor was already pinned to the pre-rebase SHA on GitHub
        # by the time the rebase ran -- a crash inside the rebase or
        # before the push would have left this signal for the next
        # tick to recover from.
        self.assertEqual(
            anchor_seen_at_rebase, ["before-sha"],
            "anchor must be persisted to GitHub BEFORE the rebase "
            f"call; saw {anchor_seen_at_rebase!r}",
        )
        # Final pinned state has the anchor cleared by the success path.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))

    def test_pr_normal_rebase_clears_anchor_on_push_failure(self) -> None:
        # The push-failure park path must clear the anchor before
        # parking, otherwise the next refresh tick's recovery branch
        # would try to push again on a state the caller already
        # reset.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=False)
        head_sha = MagicMock(side_effect=["before-sha", "after-sha"])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )

    def test_pr_unreadable_pre_rebase_head_parks_without_rebasing(
        self,
    ) -> None:
        # Fail-closed regression: when `_head_sha(worktree)` returns
        # empty BEFORE the rebase, the orchestrator must park awaiting
        # human and NOT attempt the rebase. Proceeding silently would
        # (a) write the crash-recovery anchor as `None` (no signal for
        # the next tick to recover from), (b) call `_push_branch` with
        # `force_with_lease=None`, and (c) silently treat any moved
        # HEAD as a no-op in the post-rebase check -- all of which
        # weaken the lease / recovery safeguard.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock()
        push = MagicMock()
        head_sha = MagicMock(return_value="")  # unreadable HEAD
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Rebase MUST NOT have been attempted -- we have no pre-rebase
        # SHA to anchor the recovery or the lease against.
        merge.assert_not_called()
        push.assert_not_called()
        # No label flip; the issue is parked for the operator to
        # investigate why `git rev-parse HEAD` fails on the worktree.
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_failed",
        )
        # The crash-recovery anchor is NOT set: there is no rebased
        # SHA to recover from.
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))
        # HITL message names the underlying failure.
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertIn("HEAD", self.gh.posted_comments[0][1])

    def test_pr_unreadable_post_rebase_head_resets_and_parks(self) -> None:
        # Fail-closed regression: when `_head_sha(worktree)` returns a
        # value pre-rebase but EMPTY post-rebase, the previous
        # `not after_sha or after_sha == before_sha` early return would
        # silently treat it as a no-op, clear the recovery anchor, and
        # leave the worktree on an unknown SHA. The fail-closed behavior
        # resets HEAD to `before_sha` and parks awaiting human.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock()
        # Pre-rebase HEAD readable, post-rebase HEAD unreadable.
        head_sha = MagicMock(side_effect=["before-sha", ""])
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Reset to the known pre-rebase SHA was issued so the worktree
        # is restored to a known state instead of left on whatever
        # SHA the rebase produced.
        reset_calls = [
            c for c in hardened.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(len(reset_calls), 1, hardened.call_args_list)
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_failed",
        )
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))

    def test_pr_dirty_after_crashed_rebase_reaches_recovery_branch(
        self,
    ) -> None:
        # Regression: a crash between the recovery anchor write and the
        # post-rebase dirty / push check leaves BOTH
        # `pending_auto_base_rebase_push_sha` set AND uncommitted edits
        # on the worktree. The outer `_sync_worktree_with_base` must
        # NOT hard-skip a dirty PR-having worktree -- it would otherwise
        # bypass `_recover_pending_auto_base_rebase` and let the
        # same-tick stage handler read a local SHA that is NOT on the
        # PR. Routing into `_sync_pr_worktree_to_base` is what gives
        # the recovery's case-3 reset+clean+park path the chance to
        # run.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="before-sha",
        )
        self._add_pr()
        # Worktree is dirty (the crash left uncommitted edits behind).
        # `behind == 0` because the rebased SHA already contains base.
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        merge = MagicMock()
        head_sha = MagicMock(return_value="rebased-sha")
        # Recovery's `_branch_ahead_behind`: HEAD is ahead of remote
        # PR head (push pending) -- case 3.
        ahead_behind = MagicMock(return_value=(1, 0))
        fetch = MagicMock(return_value=self._git_result())
        push = MagicMock()
        # Remote PR head is still at the pre-rebase SHA -- push
        # pending. The recovery confirms SHA mismatch via rev-parse
        # before routing to case 3.
        hardened = MagicMock(side_effect=self._hardened_for_recovery("before-sha"))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=["scratch.py"],
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Recovery's case-3 dirty branch ran: reset HEAD back to the
        # anchor and `git clean -fd`.
        reset_calls = [
            c for c in hardened.call_args_list
            if c.args[:3] == ("reset", "--hard", "before-sha")
        ]
        self.assertEqual(len(reset_calls), 1, hardened.call_args_list)
        clean_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("clean", "-fd")
        ]
        self.assertEqual(len(clean_calls), 1, hardened.call_args_list)
        # Push MUST NOT have run -- the worktree was dirty.
        push.assert_not_called()
        # Issue parked with the dirty park reason; the anchor is
        # cleared so the next refresh starts fresh on the operator's
        # human-comment reply.
        data = self.gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "auto_base_rebase_dirty")
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))

    def test_pr_stale_anchor_cleared_when_label_left_refresh_set(
        self,
    ) -> None:
        # Regression: an operator can manually relabel an issue to
        # `resolving_conflict` (or any other label outside the
        # refresh-driven set) WHILE `pending_auto_base_rebase_push_sha`
        # is still pinned from a prior interrupted auto-rebase tick.
        # Without an explicit clear here, the stale flag survives the
        # manual conflict workflow; once the issue eventually returns
        # to `validating`, recovery would try to reset / push against
        # a pre-rebase SHA that no longer matches reality. The
        # label-not-in-set early return must therefore hand off to the
        # recovery helper's cleanup branch.
        from unittest.mock import MagicMock
        # Issue is now labeled `resolving_conflict` -- NOT in the
        # `_PR_REFRESH_DETOUR_LABELS` set.
        self.gh.add_issue(make_issue(7, label="resolving_conflict"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="stale-anchor",
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        merge = MagicMock()
        push = MagicMock()
        head_sha = MagicMock()
        ahead_behind = MagicMock()
        fetch = MagicMock()
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_branch_ahead_behind", ahead_behind), \
             patch.object(base_sync, "_authed_fetch", fetch), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No rebase / no push / no relabel -- the label belongs to a
        # handler, not the refresh.
        merge.assert_not_called()
        push.assert_not_called()
        # The recovery helper's `label not in set` branch fired and
        # cleared the stale anchor. NO authed fetch of the remote PR
        # branch -- that branch is reached only on cases 1-4 inside
        # the helper, AFTER the label gate.
        fetch.assert_not_called()
        ahead_behind.assert_not_called()
        # Critical: anchor cleared so a later return to `validating`
        # does NOT trigger bogus recovery against the stale SHA.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))

    def test_pr_stale_anchor_cleared_when_pr_terminal(self) -> None:
        # Same cleanup contract for the terminal-PR early return: a
        # merged / closed PR makes the recovery target meaningless, so
        # the anchor must not survive into a possibly re-opened future.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pending_auto_base_rebase_push_sha="stale-anchor",
        )
        # Merged PR -- terminal.
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        merge = MagicMock()
        push = MagicMock()
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No rebase, no push, no relabel.
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        # Anchor cleared.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("pending_auto_base_rebase_push_sha"))

    def test_hold_base_sync_label_skips_pr_refresh_detour(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_hold_base_sync_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_backlog_label_skips_pr_refresh_detour(self) -> None:
        # `backlog` is a hard skip: the refresh path must not relabel the
        # issue to `resolving_conflict` or post a PR notice while the
        # operator has the issue postponed.
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_backlog_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_pr_having_resolving_conflict_label_does_not_re_route(self) -> None:
        # The handler runs this tick anyway and will do the rebase -- a
        # second label flip is pointless and would re-post the PR notice.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="resolving_conflict"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No new label flip (the issue was already labeled
        # `resolving_conflict` at fixture time, not by us).
        self.assertEqual(self.gh.label_history, [])
        # No duplicate PR notice.
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_having_up_to_date_does_not_route(self) -> None:
        # behind = 0 short-circuits: nothing to refresh, no detour.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_preserves_existing_conflict_round(self) -> None:
        # On a conflict-driven re-entry from a previous resolving_conflict
        # round, the cap counter must NOT reset to 0 -- mirrors
        # `_handle_in_review`'s "set when absent" semantics so a
        # perpetually-stuck PR can't ping-pong forever. The clean-rebase
        # path no longer touches `conflict_round`; this test exercises
        # the conflicted-files path where the counter is still seeded.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7", conflict_round=2,
        )
        self._add_pr()
        merge = MagicMock(return_value=(False, ["a.py"]))
        head_sha = MagicMock(return_value="before")
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        # Existing counter (2) preserved, not reset to 0.
        self.assertEqual(data.get("conflict_round"), 2)
        # The conflict path still flips to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)

    def test_pr_route_skips_merged_pr(self) -> None:
        # Regression: a just-merged PR advances `origin/<base>`, so the
        # still-in_review worktree pointed at the now-stale branch is
        # naturally behind. Without the PR-state gate the refresh would
        # post an "auto-resolution" notice and relabel the issue to
        # `resolving_conflict` on a PR the next handler call would
        # finalize to `done`. Leaving the label alone lets the existing
        # in_review terminal handler (or the closed-issue sweep variant)
        # do its job.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_closed_unmerged_pr(self) -> None:
        # Same regression for the rejected terminal: a closed-without-merge
        # PR that happens to be behind base must not be relabeled to
        # `resolving_conflict`. The handler will finalize to `rejected`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        self._add_pr(merged=False, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_when_get_pr_fails(self) -> None:
        # Defensive: if PR state cannot be determined this tick, leave the
        # label alone -- the handler can retry from a stable label rather
        # than racing a half-known state.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/acme__widget/issue-7")
        # No PR added -- get_pr will raise KeyError on the FakeGitHubClient.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_does_not_bump_in_review_watermark(self) -> None:
        # Regression: the refresh-time flow runs BEFORE any handler scans
        # comments. Bumping `pr_last_comment_id` past `latest_comment_id`
        # would silently mark unread human "do not merge" / fix-request
        # comments as consumed; the next `_handle_in_review` scan would
        # then skip them and the in_review HITL ready-ping could
        # advertise the PR as ready for human merge over the human
        # signal. The watermark must be left alone on both branches of
        # the new flow (clean rebase + conflicted rebase) -- the next
        # in_review scan will pick the human comments up correctly, and
        # the orchestrator's own PR notice is filtered via
        # `orchestrator_comment_ids` so it does not replay either.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            pr_last_comment_id=100,
        )
        self._add_pr()
        # An UNREAD human comment landed AFTER the current watermark of 100.
        # If we bump the watermark to `latest_comment_id` (max id seen, which
        # would include this human comment), it gets silently consumed.
        self.gh._issues[7].comments.append(FakeComment(
            id=500, body="do not merge yet", user=FakeUser("human"),
        ))
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before", "after"])
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        # Watermark stayed at 100 -- the unread human comment at id=500 is
        # still ahead of it and the next in_review scan will pick it up.
        self.assertEqual(data.get("pr_last_comment_id"), 100)

    def test_pr_route_skips_when_awaiting_human(self) -> None:
        # Regression: a parked PR (`awaiting_human=True`) must not be
        # detoured. `_handle_resolving_conflict`'s awaiting-human branch
        # returns early without rebasing unless a new human comment arrives,
        # so relabeling here would silently hide the existing park behind a
        # `resolving_conflict` label without making any progress -- including
        # the documented `in_review` unmergeable park path. Leaving the
        # park intact preserves its visibility and the human-driven recovery
        # path the park already invited.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/acme__widget/issue-7",
            awaiting_human=True, park_reason="unmergeable",
        )
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No relabel: park left intact.
        self.assertEqual(self.gh.label_history, [])
        # No PR notice posted (would have been duplicate noise on a parked
        # issue that already has an HITL ping).
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_skips_dirty_worktree(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=["a.py"],
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_already_up_to_date(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_rev_list_fails(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(returncode=128))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_clean_rebase_when_behind(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(True, []))
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_called_once()
        # No abort issued on success.
        self.assertFalse(
            any(c.args[:1] == ("rebase",) for c in hardened.call_args_list)
        )

    def test_conflict_aborts_and_swallows(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(False, ["a.py", "b.py"]))
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Abort issued exactly once.
        abort_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("rebase", "--abort")
        ]
        self.assertEqual(len(abort_calls), 1)

    def test_missing_issue_is_swallowed(self) -> None:
        # An orphan worktree (issue deleted on GitHub side, or fetch error)
        # must not crash the refresh -- skip silently.
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 9999)
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
