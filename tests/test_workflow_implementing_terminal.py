# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing terminal behavior."""

from __future__ import annotations

import unittest

from tests import implementing_terminal_test_support as support

CLEANUP_TERMINAL_BRANCH = support.CLEANUP_TERMINAL_BRANCH
DEV_AGENT = support.DEV_AGENT
DEV_SESSION = support.DEV_SESSION
EXTERNALLY_MERGED_ISSUE = support.EXTERNALLY_MERGED_ISSUE
EXTERNALLY_MERGED_PR = support.EXTERNALLY_MERGED_PR
FakeGitHubClient = support.FakeGitHubClient
FakePR = support.FakePR
FakePRRef = support.FakePRRef
LABEL_DONE = support.LABEL_DONE
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
PR_HEAD_SHA = support.PR_HEAD_SHA
RUN_AGENT = support.RUN_AGENT
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
_issue_branch = support._issue_branch
make_issue = support.make_issue


class HandleImplementingExternalMergeTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human merged the PR before implementing finished (e.g. an
    operator cherry-picked the work elsewhere). The handler must
    short-circuit to `done` instead of resuming the dev agent.
    """

    def test_external_merge_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(EXTERNALLY_MERGED_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        pr = FakePR(
            number=EXTERNALLY_MERGED_PR,
            head_branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            EXTERNALLY_MERGED_ISSUE,
            pr_number=EXTERNALLY_MERGED_PR,
            branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertIn((EXTERNALLY_MERGED_ISSUE, LABEL_DONE), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(EXTERNALLY_MERGED_ISSUE))
        self.assertTrue(issue.closed)
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_TERMINAL_BRANCH].assert_called_once_with(
            gh,
            _TEST_SPEC,
            EXTERNALLY_MERGED_ISSUE,
            branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
        )
