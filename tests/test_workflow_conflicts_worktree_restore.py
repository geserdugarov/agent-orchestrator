# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import workflow, worktree_lifecycle

from tests.workflow_helpers import _TEST_SPEC


class EnsurePrWorktreeRestoresFromRemoteBranchTest(unittest.TestCase):
    """When the local PR branch has been pruned (host restart, manual
    cleanup, `git branch -D`), `_ensure_pr_worktree` must restore it
    from `origin/<branch>` -- NOT from `origin/<base>`. Rebuilding from
    base would silently discard the PR's commits and the conflict
    resolution would never converge.
    """

    ISSUE_NUMBER = 300
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"

    def _git_recorder(self, *, local_branch_present: bool):
        """Return a `_git` stand-in that records every invocation and
        answers `rev-parse --verify <branch>` per the flag.
        """
        from unittest.mock import MagicMock

        calls: list[tuple] = []

        def fake_git(*args, cwd):
            calls.append((args, cwd))
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                rc = 0 if local_branch_present else 1
                return MagicMock(returncode=rc, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(side_effect=fake_git), calls

    def _authed_fetch_mock(self):
        """Return a mock for `_authed_target_fetch` that records every
        call as `(spec, branch)` and returns success. The target-root
        fetches now go through this helper rather than plain `_git`
        because the bare form relied on git's ambient credential helper
        / session state (and could not pick a per-repo token when the
        local clone has multiple GitHub-pointing remotes).
        """
        from unittest.mock import MagicMock
        fetched: list[tuple] = []

        def fake_fetch(spec, branch):
            fetched.append((spec, branch))
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(side_effect=fake_fetch), fetched

    def test_missing_branch_restores_from_origin(self) -> None:
        # The most common bad outcome: someone deletes the local branch.
        # Without our fix, `_ensure_worktree`'s fallback would create a
        # NEW branch from `origin/<base>`, discarding all the PR's
        # commits. Our helper must use `origin/<branch>` instead.
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=False)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False  # worktree dir absent too

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_authed_target_fetch", fetch_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path), \
             patch.object(worktree_lifecycle, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        # Find the `worktree add` invocation and verify it anchored on
        # `origin/<branch>`, not `origin/<base>`.
        worktree_adds = [
            args for args, _ in calls if args and args[0] == "worktree" and args[1] == "add"
        ]
        self.assertTrue(worktree_adds, "expected at least one `worktree add` call")
        add_args = worktree_adds[0]
        # Form is: ("worktree", "add", "-b", branch, str(wt), "origin/<branch>")
        self.assertEqual(add_args[2], "-b")
        self.assertEqual(add_args[3], self.BRANCH)
        self.assertEqual(add_args[5], f"origin/{self.BRANCH}")
        # NOT `origin/<base>` -- that would discard the PR's commits.
        self.assertNotEqual(add_args[5], f"origin/{_TEST_SPEC.base_branch}")

    def test_present_local_branch_uses_existing_ref(self) -> None:
        # When the local branch still exists, attach the worktree to it
        # directly (no -b restoration needed).
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=True)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_authed_target_fetch", fetch_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path), \
             patch.object(worktree_lifecycle, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        worktree_adds = [
            args for args, _ in calls if args and args[0] == "worktree" and args[1] == "add"
        ]
        self.assertTrue(worktree_adds)
        add_args = worktree_adds[0]
        # No `-b` -- attach to the existing local branch as-is.
        self.assertNotIn("-b", add_args)
        self.assertEqual(add_args[3], self.BRANCH)

    def test_non_fetch_git_calls_run_in_target_root(self) -> None:
        # All non-fetch git invocations (rev-parse, worktree add/remove)
        # must run from `spec.target_root`. Authed fetches are routed
        # via `_authed_target_fetch` which already cd's into target_root.
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=True)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_authed_target_fetch", fetch_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path), \
             patch.object(worktree_lifecycle, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        for args, cwd in calls:
            self.assertEqual(
                cwd, _TEST_SPEC.target_root,
                f"git invocation {args} ran from {cwd}, "
                f"expected {_TEST_SPEC.target_root}",
            )

    def test_branch_fetch_uses_authed_target(self) -> None:
        # `git fetch <remote> <branch>` in target_root used to relyon git's
        # ambient credential helper; `_authed_target_fetch` replaces it
        # with an askpass-delivered per-spec token. The branch fetch and
        # the base-branch fetch must both go through the helper, and
        # neither must surface as a plain `_git("fetch", ...)` call.
        from unittest.mock import MagicMock

        git_mock, git_calls = self._git_recorder(local_branch_present=True)
        fetch_mock, fetched = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_authed_target_fetch", fetch_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path), \
             patch.object(worktree_lifecycle, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        # Both fetches landed on the authed helper -- base and PR branch.
        self.assertEqual(len(fetched), 2)
        branches = {branch for _spec, branch in fetched}
        self.assertEqual(branches, {_TEST_SPEC.base_branch, self.BRANCH})
        # And no plain-git fetch leaked through (which would prompt for
        # credentials under systemd and fail).
        for args, _cwd in git_calls:
            self.assertNotEqual(
                args[0] if args else "", "fetch",
                f"plain `_git(\"fetch\", ...)` leaked: {args!r}",
            )


if __name__ == "__main__":
    unittest.main()
