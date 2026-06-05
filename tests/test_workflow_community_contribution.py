# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.github import COMMUNITY_CONTRIBUTION_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, FakePR, FakeUser
from tests.workflow_helpers import _TEST_SPEC


def _pr(number: int, *, author: str, labels=()) -> FakePR:
    return FakePR(
        number=number,
        user=FakeUser(author),
        labels=[FakeLabel(name) for name in labels],
    )


class SweepCommunityContributionPRsTest(unittest.TestCase):
    """`_sweep_community_contribution_prs` labels open PRs whose authors
    are not in `ALLOWED_ISSUE_AUTHORS` and posts a HITL ping comment once
    per PR. With an empty allowlist the sweep is a no-op so the legacy
    "anyone is trusted" deployment is unchanged.
    """

    def test_no_op_when_allowlist_is_empty(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author="outsider"))
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ()):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.pulls[1].labels, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_outsider_pr_gets_labeled_and_hitl_pinged(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(7, author="outsider"))
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)), \
             patch.object(config, "HITL_MENTIONS", "@geserdugarov"):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertTrue(
            gh.pr_has_label(gh.pulls[7], COMMUNITY_CONTRIBUTION_LABEL)
        )
        self.assertEqual(len(gh.posted_pr_comments), 1)
        pr_number, body = gh.posted_pr_comments[0]
        self.assertEqual(pr_number, 7)
        self.assertIn("@geserdugarov", body)
        self.assertIn("@outsider", body)

    def test_allowed_author_is_skipped(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author="geserdugarov"))
        gh.add_pr(_pr(2, author="Geserdugarov"))  # case-insensitive
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.pulls[1].labels, [])
        self.assertEqual(gh.pulls[2].labels, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_idempotent_does_not_re_ping_already_labeled_prs(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(
            _pr(3, author="outsider", labels=(COMMUNITY_CONTRIBUTION_LABEL,))
        )
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        # Still labeled exactly once, no duplicate comment.
        names = [l.name for l in gh.pulls[3].labels]
        self.assertEqual(names.count(COMMUNITY_CONTRIBUTION_LABEL), 1)
        self.assertEqual(gh.posted_pr_comments, [])

    def test_one_pr_failure_does_not_stop_sweep(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author="outsider-a"))
        gh.add_pr(_pr(2, author="outsider-b"))
        calls: list[int] = []
        original = gh.add_pr_label

        def boom(pr, label):
            calls.append(pr.number)
            if pr.number == 1:
                raise RuntimeError("boom")
            original(pr, label)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)), \
             patch.object(gh, "add_pr_label", side_effect=boom):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        # Both PRs were attempted (the failure on #1 must not abort the
        # sweep). Both got a HITL ping because the comment is posted
        # BEFORE the label; only #2 ended up labeled because #1's label
        # write raised. #1 stays un-labeled on purpose so the next tick
        # retries the ping rather than silently skipping the PR.
        self.assertEqual(sorted(calls), [1, 2])
        self.assertFalse(
            gh.pr_has_label(gh.pulls[1], COMMUNITY_CONTRIBUTION_LABEL)
        )
        self.assertTrue(
            gh.pr_has_label(gh.pulls[2], COMMUNITY_CONTRIBUTION_LABEL)
        )
        self.assertEqual(
            sorted(n for n, _ in gh.posted_pr_comments), [1, 2]
        )

    def test_comment_failure_leaves_pr_unlabeled_for_retry(self) -> None:
        # Regression: the label is the dedup marker that suppresses
        # re-pinging on later ticks. If `pr_comment` raises, the label
        # must NOT be written -- otherwise the PR is silently skipped on
        # the next tick and no human is ever called.
        gh = FakeGitHubClient()
        gh.add_pr(_pr(11, author="outsider"))
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)), \
             patch.object(
                gh, "pr_comment", side_effect=RuntimeError("comment boom")
             ):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertFalse(
            gh.pr_has_label(gh.pulls[11], COMMUNITY_CONTRIBUTION_LABEL)
        )
        # A subsequent tick (comment now succeeds) must complete both
        # writes against the same PR, proving the retry path works.
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertTrue(
            gh.pr_has_label(gh.pulls[11], COMMUNITY_CONTRIBUTION_LABEL)
        )
        self.assertEqual([n for n, _ in gh.posted_pr_comments], [11])

    def test_enumeration_failure_is_swallowed(self) -> None:
        gh = FakeGitHubClient()
        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)), \
             patch.object(
                gh, "iter_open_prs", side_effect=RuntimeError("api boom")
             ):
            # Must not raise.
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.posted_pr_comments, [])


class TickInvokesSweepTest(unittest.TestCase):
    """`workflow.tick` must drive the community-contribution sweep on
    every tick so a newly-opened outsider PR is labeled without the
    operator having to take action.
    """

    def test_tick_calls_sweep_after_refresh(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        refresh = MagicMock()
        sweep = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_sweep_community_contribution_prs", sweep):
            workflow.tick(gh, _TEST_SPEC)
        sweep.assert_called_once_with(gh, _TEST_SPEC)


if __name__ == "__main__":
    unittest.main()
