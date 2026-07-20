# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow, worktree_lifecycle
from orchestrator.github import GitHubClient
from github import GithubException

from tests.fakes import FakeGitHubClient
from tests.workflow_helpers import _TEST_SPEC

CLEANUP_ISSUE_NUMBER = 99
CLEANUP_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-99"


def _git_result(*, returncode: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stderr=stderr, stdout="")


class _CleanupGit:
    def __init__(self, *, local_branch_exists: bool, fail_deletes: bool = False):
        self._rev_parse_returncode = 0 if local_branch_exists else 1
        self._fail_deletes = fail_deletes

    def __call__(self, *args, cwd):
        command = args[0]
        if command == "rev-parse":
            return _git_result(returncode=self._rev_parse_returncode)
        if self._fail_deletes:
            return _git_result(returncode=1, stderr="boom")
        return _git_result()


def _raising_delete(_branch: str) -> None:
    raise RuntimeError("api went away")


def _fake_worktree_path(*, exists: bool, path: str) -> MagicMock:
    worktree_path = MagicMock()
    worktree_path.exists.return_value = exists
    worktree_path.__str__ = lambda _self: path
    return worktree_path


def _run_cleanup(*, worktree_exists: bool, local_branch_exists: bool):
    gh = FakeGitHubClient()
    git_mock = MagicMock(side_effect=_CleanupGit(
        local_branch_exists=local_branch_exists,
    ))
    worktree_path = _fake_worktree_path(
        exists=worktree_exists,
        path=f"/tmp/issue-{CLEANUP_ISSUE_NUMBER}",
    )
    with patch.object(worktree_lifecycle, "_git", git_mock), patch.object(
        worktree_lifecycle,
        "_worktree_path",
        return_value=worktree_path,
    ):
        workflow._cleanup_terminal_branch(
            gh,
            _TEST_SPEC,
            CLEANUP_ISSUE_NUMBER,
        )
    return gh, git_mock


def _client_with_ref(*, raise_status):
    client = GitHubClient.__new__(GitHubClient)
    client.repo = MagicMock()
    git_ref = MagicMock()
    client.repo.get_git_ref.return_value = git_ref
    if raise_status is not None:
        error = GithubException(status=raise_status, data={"message": "x"})
        git_ref.delete.side_effect = error
    return client


class CleanupTerminalBranchTest(unittest.TestCase):
    """Direct coverage of `_cleanup_terminal_branch`. The handler-level
    tests patch this helper out so they only check it was invoked; here we
    run the real implementation with `_git` mocked to verify the worktree
    removal, local branch delete, and remote branch delete each fire (and
    that an absent worktree is silently skipped instead of erroring). Also
    verifies the helper never raises on subprocess / remote failures, so
    a cleanup hiccup cannot block the terminal label flip in the caller.
    """

    def test_full_cleanup_runs_all_three_steps(self) -> None:
        gh, git_mock = _run_cleanup(
            worktree_exists=True, local_branch_exists=True,
        )

        # Worktree remove issued first, then rev-parse to probe the local
        # branch, then `branch -D`. The remote-side delete recorder confirms
        # gh.delete_remote_branch was called with the per-issue branch.
        cmds = [call.args[0] for call in git_mock.call_args_list]
        self.assertEqual(
            cmds[:3],
            ["worktree", "rev-parse", "branch"],
        )
        # The branch -D invocation targets the per-issue branch by name.
        branch_call = next(
            call for call in git_mock.call_args_list if call.args[0] == "branch"
        )
        self.assertEqual(branch_call.args[1], "-D")
        self.assertEqual(branch_call.args[2], CLEANUP_BRANCH)
        self.assertEqual(gh.deleted_remote_branches, [CLEANUP_BRANCH])

    def test_skips_remove_without_worktree(self) -> None:
        # Worktree may already be gone if the operator cleaned it up by hand
        # or a prior tick removed it. Helper should still drop the local
        # branch and request the remote delete instead of erroring out.
        gh, git_mock = _run_cleanup(
            worktree_exists=False, local_branch_exists=True,
        )

        cmds = [call.args[0] for call in git_mock.call_args_list]
        self.assertNotIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [CLEANUP_BRANCH])

    def test_skips_local_delete_when_branch_absent(self) -> None:
        # Branch may already be gone if a previous cleanup partly succeeded
        # or the operator pruned it. We must not run `branch -D` (it would
        # fail loudly), but must still request the remote delete.
        gh, git_mock = _run_cleanup(
            worktree_exists=True, local_branch_exists=False,
        )

        cmds = [call.args[0] for call in git_mock.call_args_list]
        self.assertIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertNotIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [CLEANUP_BRANCH])

    def test_swallows_all_failures(self) -> None:
        # Every step is best-effort: worktree-remove failure, branch -D
        # failure, and a raising remote-delete must all be absorbed so a
        # cleanup hiccup cannot block the caller (which has already
        # written the terminal pinned state). Regression guard for the
        # "no runtime exception should escape cleanup" contract.
        gh = FakeGitHubClient()
        git_mock = MagicMock(side_effect=_CleanupGit(
            local_branch_exists=True,
            fail_deletes=True,
        ))
        gh.delete_remote_branch = _raising_delete
        wt_path = _fake_worktree_path(
            exists=True,
            path=f"/tmp/issue-{CLEANUP_ISSUE_NUMBER}",
        )

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every sub-step failed.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, CLEANUP_ISSUE_NUMBER,
            )

    def test_swallows_git_subprocess_exceptions(self) -> None:
        # `_git` can raise (missing `spec.target_root`, missing `git`
        # binary, OSError) rather than returning a non-zero result. The
        # helper must swallow those too so that a worktree-remove or
        # rev-parse raise cannot skip the remote-delete step, which is
        # what the operator actually sees in the repo's branch list.
        gh = FakeGitHubClient()

        git_mock = MagicMock(side_effect=OSError("git not found"))

        wt_path = _fake_worktree_path(
            exists=True,
            path=f"/tmp/issue-{CLEANUP_ISSUE_NUMBER}",
        )

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every `_git` invocation throws.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, CLEANUP_ISSUE_NUMBER,
            )

        # The remote-delete still ran -- a local-side raise must not
        # block tidying the GitHub side.
        self.assertEqual(gh.deleted_remote_branches, [CLEANUP_BRANCH])


class CleanupDecomposeWorktreeTest(unittest.TestCase):
    """`_cleanup_decompose_worktree` runs from `_handle_decomposing`'s
    `finally`, so it must never raise -- a cleanup failure would mask the
    handler's original error. Every step, including resolving the worktree
    path, rides the best-effort guard.
    """

    ISSUE_NUMBER = 77

    def test_swallows_path_resolution_failure(self) -> None:
        # Path resolution rides inside the best-effort guard: a raise here
        # (e.g. a malformed spec) must be logged, not propagated, or it
        # would mask the decomposing handler's real failure.
        with patch.object(
            worktree_lifecycle, "_decompose_worktree_path",
            side_effect=RuntimeError("bad spec"),
        ):
            # Must NOT raise.
            worktree_lifecycle._cleanup_decompose_worktree(
                _TEST_SPEC, self.ISSUE_NUMBER,
            )

    def test_swallows_git_removal_failure(self) -> None:
        # A raising `_git` (missing binary, OSError) during the removal is
        # absorbed the same way, so a cleanup hiccup never escapes.
        wt_path = _fake_worktree_path(
            exists=True,
            path="/tmp/decompose-77",
        )

        with patch.object(
            worktree_lifecycle, "_decompose_worktree_path",
            return_value=wt_path,
        ), patch.object(
            worktree_lifecycle, "_git", side_effect=OSError("git not found"),
        ):
            # Must NOT raise even though the git removal throws.
            worktree_lifecycle._cleanup_decompose_worktree(
                _TEST_SPEC, self.ISSUE_NUMBER,
            )


class DeleteRemoteBranchTest(unittest.TestCase):
    """`GitHubClient.delete_remote_branch` is idempotent against a 404
    because the repo's "auto-delete head branches" setting may have
    already removed the ref as part of the merge. Other failures log
    and return False so the caller can keep going.
    """

    def test_success(self) -> None:
        client = _client_with_ref(raise_status=None)
        self.assertTrue(client.delete_remote_branch("orchestrator/geserdugarov__agent-orchestrator/issue-1"))
        client.repo.get_git_ref.assert_called_once_with(
            "heads/orchestrator/geserdugarov__agent-orchestrator/issue-1"
        )

    def test_404_treated_as_success(self) -> None:
        client = _client_with_ref(raise_status=404)
        self.assertTrue(client.delete_remote_branch("orchestrator/geserdugarov__agent-orchestrator/issue-1"))

    def test_other_error_returns_false(self) -> None:
        client = _client_with_ref(raise_status=403)
        self.assertFalse(client.delete_remote_branch("orchestrator/geserdugarov__agent-orchestrator/issue-1"))


if __name__ == "__main__":
    unittest.main()
