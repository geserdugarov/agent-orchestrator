# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Community-contribution failure isolation and tick wiring."""
from __future__ import annotations

import unittest
from functools import partial
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


class SweepCommunityContributionFailuresTest(unittest.TestCase):
    """One GitHub failure does not suppress later PRs or future retries."""

    def test_one_pr_failure_does_not_stop_sweep(self) -> None:
        gh = FakeGitHubClient()
        gh.add_pr(_pr(1, author="outsider-a"))
        gh.add_pr(_pr(2, author="outsider-b"))
        calls: list[int] = []
        original = gh.add_pr_label

        with (
            patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)),
            patch.object(
                gh,
                "add_pr_label",
                side_effect=partial(_fail_first_label_write, calls, original),
            ),
        ):
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
            sorted(number for number, _ in gh.posted_pr_comments), [1, 2]
        )

    def test_comment_error_leaves_pr_for_retry(self) -> None:
        # Regression: the label is the dedup marker that suppresses
        # re-pinging on later ticks. If `pr_comment` raises, the label
        # must NOT be written -- otherwise the PR is silently skipped on
        # the next tick and no human is ever called.
        gh = FakeGitHubClient()
        gh.add_pr(_pr(_COMMENT_RETRY_PR_NUMBER, author=_OUTSIDER_LOGIN))
        with (
            patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)),
            patch.object(
                gh,
                "pr_comment",
                side_effect=RuntimeError("comment boom"),
            ),
        ):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertFalse(
            gh.pr_has_label(
                gh.pulls[_COMMENT_RETRY_PR_NUMBER],
                COMMUNITY_CONTRIBUTION_LABEL,
            )
        )
        # A subsequent tick (comment now succeeds) must complete both
        # writes against the same PR, proving the retry path works.
        with patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)):
            workflow._sweep_community_contribution_prs(gh, _TEST_SPEC)
        self.assertTrue(
            gh.pr_has_label(
                gh.pulls[_COMMENT_RETRY_PR_NUMBER],
                COMMUNITY_CONTRIBUTION_LABEL,
            )
        )
        self.assertEqual(
            [number for number, _ in gh.posted_pr_comments],
            [_COMMENT_RETRY_PR_NUMBER],
        )

    def test_enumeration_failure_is_swallowed(self) -> None:
        gh = FakeGitHubClient()
        with (
            patch.object(config, _ALLOWLIST_CONFIG, (_ALLOWED_LOGIN,)),
            patch.object(
                gh,
                "iter_open_prs",
                side_effect=RuntimeError("api boom"),
            ),
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
