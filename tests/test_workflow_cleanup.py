# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow, worktree_lifecycle

from tests.fakes import FakeGitHubClient
from tests.workflow_helpers import _TEST_SPEC


class CleanupTerminalBranchTest(unittest.TestCase):
    """Direct coverage of `_cleanup_terminal_branch`. The handler-level
    tests patch this helper out so they only check it was invoked; here we
    run the real implementation with `_git` mocked to verify the worktree
    removal, local branch delete, and remote branch delete each fire (and
    that an absent worktree is silently skipped instead of erroring). Also
    verifies the helper never raises on subprocess / remote failures, so
    a cleanup hiccup cannot block the terminal label flip in the caller.
    """

    ISSUE_NUMBER = 99
    BRANCH = "orchestrator/issue-99"

    def _run_helper(
        self,
        *,
        worktree_exists: bool,
        local_branch_exists: bool,
    ):
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        rev_parse_rc = 0 if local_branch_exists else 1

        def fake_git(*args, cwd):
            cmd = args[0]
            if cmd == "worktree":
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == "rev-parse":
                return MagicMock(returncode=rev_parse_rc, stderr="", stdout="")
            if cmd == "branch":
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        git_mock = MagicMock(side_effect=fake_git)

        # `_worktree_path` returns a Path that may or may not exist on disk;
        # patch its existence check rather than touching the real filesystem.
        wt_path = MagicMock()
        wt_path.exists.return_value = worktree_exists
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            workflow._cleanup_terminal_branch(gh, _TEST_SPEC, self.ISSUE_NUMBER)
        return gh, git_mock

    def test_full_cleanup_runs_all_three_steps(self) -> None:
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=True,
        )

        # Worktree remove issued first, then rev-parse to probe the local
        # branch, then `branch -D`. The remote-side delete recorder confirms
        # gh.delete_remote_branch was called with the per-issue branch.
        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertEqual(
            cmds[:3],
            ["worktree", "rev-parse", "branch"],
        )
        # The branch -D invocation targets the per-issue branch by name.
        branch_call = next(
            c for c in git_mock.call_args_list if c.args[0] == "branch"
        )
        self.assertEqual(branch_call.args[1], "-D")
        self.assertEqual(branch_call.args[2], self.BRANCH)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_worktree_remove_when_worktree_absent(self) -> None:
        # Worktree may already be gone if the operator cleaned it up by hand
        # or a prior tick removed it. Helper should still drop the local
        # branch and request the remote delete instead of erroring out.
        gh, git_mock = self._run_helper(
            worktree_exists=False, local_branch_exists=True,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertNotIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_local_delete_when_branch_absent(self) -> None:
        # Branch may already be gone if a previous cleanup partly succeeded
        # or the operator pruned it. We must not run `branch -D` (it would
        # fail loudly), but must still request the remote delete.
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=False,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertNotIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_swallows_all_failures(self) -> None:
        # Every step is best-effort: worktree-remove failure, branch -D
        # failure, and a raising remote-delete must all be absorbed so a
        # cleanup hiccup cannot block the caller (which has already
        # written the terminal pinned state). Regression guard for the
        # "no runtime exception should escape cleanup" contract.
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        def fake_git(*args, cwd):
            cmd = args[0]
            # rev-parse returns 0 so we proceed to `branch -D`; both the
            # worktree and branch deletions return non-zero stderr so we
            # exercise both warning paths.
            if cmd == "rev-parse":
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=1, stderr="boom", stdout="")

        git_mock = MagicMock(side_effect=fake_git)

        def raising_delete(branch):  # noqa: ARG001
            raise RuntimeError("api went away")

        gh.delete_remote_branch = raising_delete

        wt_path = MagicMock()
        wt_path.exists.return_value = True
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every sub-step failed.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, self.ISSUE_NUMBER,
            )

    def test_swallows_git_subprocess_exceptions(self) -> None:
        # `_git` can raise (missing `spec.target_root`, missing `git`
        # binary, OSError) rather than returning a non-zero result. The
        # helper must swallow those too so that a worktree-remove or
        # rev-parse raise cannot skip the remote-delete step, which is
        # what the operator actually sees in the repo's branch list.
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        git_mock = MagicMock(side_effect=OSError("git not found"))

        wt_path = MagicMock()
        wt_path.exists.return_value = True
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every `_git` invocation throws.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, self.ISSUE_NUMBER,
            )

        # The remote-delete still ran -- a local-side raise must not
        # block tidying the GitHub side.
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])


class DeleteRemoteBranchTest(unittest.TestCase):
    """`GitHubClient.delete_remote_branch` is idempotent against a 404
    because the repo's "auto-delete head branches" setting may have
    already removed the ref as part of the merge. Other failures log
    and return False so the caller can keep going.
    """

    def _client_with_ref(self, *, raise_status):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        if raise_status is None:
            client.repo.get_git_ref.return_value = MagicMock()
        else:
            err = GithubException(status=raise_status, data={"message": "x"})
            client.repo.get_git_ref.return_value = MagicMock()
            client.repo.get_git_ref.return_value.delete.side_effect = err
        return client

    def test_success(self) -> None:
        client = self._client_with_ref(raise_status=None)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))
        client.repo.get_git_ref.assert_called_once_with(
            "heads/orchestrator/issue-1"
        )

    def test_404_treated_as_success(self) -> None:
        client = self._client_with_ref(raise_status=404)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))

    def test_other_error_returns_false(self) -> None:
        client = self._client_with_ref(raise_status=403)
        self.assertFalse(client.delete_remote_branch("orchestrator/issue-1"))


if __name__ == "__main__":
    unittest.main()
