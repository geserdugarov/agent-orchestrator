# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Community-contribution classification and deduplication."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.github import COMMUNITY_CONTRIBUTION_LABEL

from tests.fakes import FakeGitHubClient
from tests.workflow_community_test_support import (
    ALLOWED_LOGIN,
    ALLOWLIST_CONFIG,
    COMMENT_RETRY_PR_NUMBER,
    OUTSIDER_LOGIN,
    TEST_SPEC,
    fail_first_label_write,
    make_pr,
)


_ALLOWED_LOGIN = ALLOWED_LOGIN
_ALLOWLIST_CONFIG = ALLOWLIST_CONFIG
_COMMENT_RETRY_PR_NUMBER = COMMENT_RETRY_PR_NUMBER
_OUTSIDER_LOGIN = OUTSIDER_LOGIN
_TEST_SPEC = TEST_SPEC
_fail_first_label_write = fail_first_label_write
_pr = make_pr


class SweepCommunityContributionPRsTest(unittest.TestCase):
    """`_sweep_community_contribution_prs` labels open PRs whose authors
    are not in `ALLOWED_ISSUE_AUTHORS` and posts a HITL ping comment once
    per PR. With an empty allowlist the sweep is a no-op so the legacy
    "anyone is trusted" deployment is unchanged.
    """

    def test_no_op_when_allowlist_is_empty(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author=_OUTSIDER_LOGIN))
        with patch.object(config, _ALLOWLIST_CONFIG, ()):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.pulls[1].labels, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_outsider_pr_gets_labeled_and_hitl_pinged(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(7, author=_OUTSIDER_LOGIN))
        with patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)), \
             patch.object(config, "HITL_MENTIONS", f"@{_ALLOWED_LOGIN}"):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertTrue(
            gh.pr_has_label(gh.pulls[7], COMMUNITY_CONTRIBUTION_LABEL)
        )
        self.assertEqual(len(gh.posted_pr_comments), 1)
        pr_number, body = gh.posted_pr_comments[0]
        self.assertEqual(pr_number, 7)
        self.assertIn(f"@{_ALLOWED_LOGIN}", body)
        self.assertIn(f"@{_OUTSIDER_LOGIN}", body)

    def test_allowed_author_is_skipped(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author=_ALLOWED_LOGIN))
        gh.add_pr(_pr(2, author="Geserdugarov"))  # case-insensitive
        with patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.pulls[1].labels, [])
        self.assertEqual(gh.pulls[2].labels, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_bot_authored_pr_is_skipped(self) -> None:
        # Dependabot (and other Bot-account) PRs open structurally and are
        # not community contributions. They must not earn the label or a
        # HITL ping even though their author is outside the allowlist.
        gh = FakeGitHubClient()
        gh.add_pr(
            _pr(5, author="dependabot[bot]", user_type="Bot")
        )
        with patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertEqual(gh.pulls[5].labels, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_labeled_prs_are_not_pinged_again(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(
            _pr(
                3,
                author=_OUTSIDER_LOGIN,
                labels=(COMMUNITY_CONTRIBUTION_LABEL,),
            )
        )
        with patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        # Still labeled exactly once, no duplicate comment.
        names = [label.name for label in gh.pulls[3].labels]
        self.assertEqual(names.count(COMMUNITY_CONTRIBUTION_LABEL), 1)
        self.assertEqual(gh.posted_pr_comments, [])
