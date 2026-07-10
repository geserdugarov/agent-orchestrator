# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest



class CreateChildIssueAlwaysUsesParentRepoTest(unittest.TestCase):
    """`create_child_issue` is structurally bound to `self.repo` so a
    misuse cannot accidentally file a child against a different repo
    than the parent. Worth a regression test anyway.
    """

    def test_creates_child_in_repo_with_parent_link(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        sentinel = MagicMock(name="created_issue")
        client.repo.create_issue.return_value = sentinel

        out = client.create_child_issue(
            title="A", body="do A", parent_number=42, labels=["ready"],
        )

        self.assertIs(out, sentinel)
        client.repo.create_issue.assert_called_once()
        kwargs = client.repo.create_issue.call_args.kwargs
        self.assertEqual(kwargs["title"], "A")
        self.assertEqual(kwargs["labels"], ["ready"])
        # Parent link prepended via the helper (not by the caller) so the
        # workflow code can hand the agent's raw body straight in.
        self.assertIn("Parent: #42", kwargs["body"])
