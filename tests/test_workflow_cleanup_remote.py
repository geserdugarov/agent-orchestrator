# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from orchestrator.github import GitHubClient
from github import GithubException


CLEANUP_ISSUE_NUMBER = 99
CLEANUP_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-99"
DECOMPOSE_ISSUE_NUMBER = 77
REMOTE_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-1"
REMOTE_REF = f"heads/{REMOTE_BRANCH}"
WORKTREE_COMMAND = "worktree"
REV_PARSE_COMMAND = "rev-parse"
BRANCH_COMMAND = "branch"
GIT_HELPER_ATTR = "_git"
NOT_FOUND_STATUS = 404
FORBIDDEN_STATUS = 403


def _client_with_ref(*, raise_status):
    client = GitHubClient.__new__(GitHubClient)
    client.repo = MagicMock()
    git_ref = MagicMock()
    client.repo.get_git_ref.return_value = git_ref
    if raise_status is not None:
        error = GithubException(status=raise_status, data={"message": "x"})
        git_ref.delete.side_effect = error
    return client


class DeleteRemoteBranchTest(unittest.TestCase):
    """`GitHubClient.delete_remote_branch` is idempotent against a 404
    because the repo's "auto-delete head branches" setting may have
    already removed the ref as part of the merge. Other failures log
    and return False so the caller can keep going.
    """

    def test_success(self) -> None:
        client = _client_with_ref(raise_status=None)
        self.assertTrue(client.delete_remote_branch(REMOTE_BRANCH))
        client.repo.get_git_ref.assert_called_once_with(REMOTE_REF)

    def test_missing_ref_treated_as_success(self) -> None:
        client = _client_with_ref(raise_status=NOT_FOUND_STATUS)
        self.assertTrue(client.delete_remote_branch(REMOTE_BRANCH))

    def test_other_error_returns_false(self) -> None:
        client = _client_with_ref(raise_status=FORBIDDEN_STATUS)
        self.assertFalse(client.delete_remote_branch(REMOTE_BRANCH))


if __name__ == "__main__":
    unittest.main()
