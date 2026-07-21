# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow, worktree_lifecycle

from tests.workflow_helpers import _TEST_SPEC

WORKTREE_ISSUE = 300
WORKTREE_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"


class _GitRecorder:
    def __init__(self, *, local_branch_present: bool):
        self.local_branch_present = local_branch_present
        self.calls = []

    def __call__(self, *args, cwd):
        self.calls.append((args, cwd))
        if args and args[0] == "rev-parse":
            return MagicMock(
                returncode=0 if self.local_branch_present else 1,
                stdout="",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")


class _AuthedFetchRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, spec, branch):
        self.calls.append((spec, branch))
        return MagicMock(returncode=0, stdout="", stderr="")


class _WorktreeRestoreFixtureMixin:
    def _run_ensure(self, *, local_branch_present: bool):
        git_recorder = _GitRecorder(
            local_branch_present=local_branch_present,
        )
        fetch_recorder = _AuthedFetchRecorder()
        worktree_path = MagicMock()
        worktree_path.exists.return_value = False

        with (
            patch.object(worktree_lifecycle, "_git", git_recorder),
            patch.object(
                worktree_lifecycle,
                "_authed_target_fetch",
                fetch_recorder,
            ),
            patch.object(
                worktree_lifecycle,
                "_worktree_path",
                return_value=worktree_path,
            ),
            patch.object(
                worktree_lifecycle,
                "_repo_worktrees_root",
                return_value=MagicMock(),
            ),
        ):
            workflow._ensure_pr_worktree(_TEST_SPEC, WORKTREE_ISSUE)
        return git_recorder.calls, fetch_recorder.calls


class EnsurePrWorktreeRestoresFromRemoteBranchTest(
    unittest.TestCase,
    _WorktreeRestoreFixtureMixin,
):
    """Restore missing PR worktrees from the remote PR branch."""

    def test_missing_branch_restores_from_origin(self) -> None:
        # The most common bad outcome: someone deletes the local branch.
        # Without our fix, `_ensure_worktree`'s fallback would create a
        # NEW branch from `origin/<base>`, discarding all the PR's
        # commits. Our helper must use `origin/<branch>` instead.
        git_calls, _ = self._run_ensure(local_branch_present=False)

        # Find the `worktree add` invocation and verify it anchored on
        # `origin/<branch>`, not `origin/<base>`.
        worktree_adds = [
            args
            for args, _ in git_calls
            if args and args[:2] == ("worktree", "add")
        ]
        self.assertTrue(worktree_adds, "expected at least one `worktree add` call")
        add_args = worktree_adds[0]
        # Form is: ("worktree", "add", "-b", branch, str(wt), "origin/<branch>")
        self.assertEqual(add_args[2], "-b")
        self.assertEqual(add_args[3], WORKTREE_BRANCH)
        self.assertEqual(add_args[5], f"origin/{WORKTREE_BRANCH}")
        # NOT `origin/<base>` -- that would discard the PR's commits.
        self.assertNotEqual(add_args[5], f"origin/{_TEST_SPEC.base_branch}")

    def test_present_local_branch_uses_existing_ref(self) -> None:
        # When the local branch still exists, attach the worktree to it
        # directly (no -b restoration needed).
        git_calls, _ = self._run_ensure(local_branch_present=True)

        worktree_adds = [
            args
            for args, _ in git_calls
            if args and args[:2] == ("worktree", "add")
        ]
        self.assertTrue(worktree_adds)
        add_args = worktree_adds[0]
        # No `-b` -- attach to the existing local branch as-is.
        self.assertNotIn("-b", add_args)
        self.assertEqual(add_args[3], WORKTREE_BRANCH)

    def test_non_fetch_git_calls_run_in_target_root(self) -> None:
        # All non-fetch git invocations (rev-parse, worktree add/remove)
        # must run from `spec.target_root`. Authed fetches are routed
        # via `_authed_target_fetch` which already cd's into target_root.
        git_calls, _ = self._run_ensure(local_branch_present=True)

        for args, cwd in git_calls:
            self.assertEqual(
                cwd,
                _TEST_SPEC.target_root,
                f"git invocation {args} ran from {cwd}, expected {_TEST_SPEC.target_root}",
            )

    def test_branch_fetch_uses_authed_target(self) -> None:
        # `git fetch <remote> <branch>` in target_root used to relyon git's
        # ambient credential helper; `_authed_target_fetch` replaces it
        # with an askpass-delivered per-spec token. The branch fetch and
        # the base-branch fetch must both go through the helper, and
        # neither must surface as a plain `_git("fetch", ...)` call.
        git_calls, fetch_calls = self._run_ensure(local_branch_present=True)

        # Both fetches landed on the authed helper -- base and PR branch.
        self.assertEqual(len(fetch_calls), 2)
        branches = {branch for _spec, branch in fetch_calls}
        self.assertEqual(branches, {_TEST_SPEC.base_branch, WORKTREE_BRANCH})
        # And no plain-git fetch leaked through (which would prompt for
        # credentials under systemd and fail).
        for args, _cwd in git_calls:
            self.assertNotEqual(
                args[0] if args else "",
                "fetch",
                f'plain `_git("fetch", ...)` leaked: {args!r}',
            )


if __name__ == "__main__":
    unittest.main()
