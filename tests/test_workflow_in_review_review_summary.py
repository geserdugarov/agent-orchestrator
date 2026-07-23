# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Routing tests for fresh pull-request review-summary feedback."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config

from tests.fakes import FakeGitHubClient, FakePR, FakePRRef, FakePRReview, FakeUser, make_issue
from tests.workflow_helpers import _PatchedWorkflowMixin, _agent, _issue_branch

REVIEW_SUMMARY_ISSUE = 90
REVIEW_SUMMARY_PR = 130
REVIEW_SUMMARY_WATERMARK = 999
CHANGE_REQUEST_REVIEW_ID = 4242
COMMENT_REVIEW_ID = 4243
APPROVAL_REVIEW_ID = 4244
EMPTY_REVIEW_ID = 4245
READY_MESSAGE = "ready for review/merge"
REVIEWED_SHA = "cafe1234"
CHECKS_SUCCESS = "success"
RUN_AGENT = "run_agent"
HUMAN_LOGIN = "alice"
LABEL_FIXING = "fixing"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"
REVIEW_DEBOUNCE_SECONDS = 600


def _hour_ago():
    return datetime.now(timezone.utc) - timedelta(hours=1)


@dataclass(frozen=True)
class _ReviewScenario:
    github: FakeGitHubClient
    issue: object
    pull_request: FakePR


class _ReviewSummaryFixtureMixin(_PatchedWorkflowMixin):
    def _setup_with_review(self, review):
        github = FakeGitHubClient()
        issue = make_issue(REVIEW_SUMMARY_ISSUE, label="in_review")
        github.add_issue(issue)
        pull_request = FakePR(
            number=REVIEW_SUMMARY_PR,
            head_branch=_issue_branch(REVIEW_SUMMARY_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            reviews=[review],
        )
        github.add_pr(pull_request)
        github.seed_state(
            REVIEW_SUMMARY_ISSUE,
            pr_number=REVIEW_SUMMARY_PR,
            branch=_issue_branch(REVIEW_SUMMARY_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=REVIEW_SUMMARY_WATERMARK,
            pr_last_review_summary_id=0,
        )
        return _ReviewScenario(github, issue, pull_request)


class InReviewPRReviewSummaryTest(
    unittest.TestCase,
    _ReviewSummaryFixtureMixin,
):
    """Route actionable review-summary bodies while ignoring approvals."""

    def test_change_request_body_enters_fixing(self) -> None:
        scenario = self._setup_with_review(
            FakePRReview(
                id=CHANGE_REQUEST_REVIEW_ID,
                body="please rename foo to bar in the public API",
                state="CHANGES_REQUESTED",
                user=FakeUser(HUMAN_LOGIN),
                submitted_at=_hour_ago(),
                commit_id=REVIEWED_SHA,
            ),
        )
        github = scenario.github

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                github,
                scenario.issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), github.label_history)
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, "validating"), github.label_history)
        self.assertEqual(github.merge_calls, [])
        state = github.pinned_data(REVIEW_SUMMARY_ISSUE)
        self.assertEqual(state.get("pending_fix_review_summary_max_id"), CHANGE_REQUEST_REVIEW_ID)
        self.assertEqual(state.get("pr_last_review_summary_id"), 0)

    def test_comment_review_body_enters_fixing(self) -> None:
        scenario = self._setup_with_review(
            FakePRReview(
                id=COMMENT_REVIEW_ID,
                body="how about adding a smoke test for the empty-input case?",
                state="COMMENTED",
                user=FakeUser(HUMAN_LOGIN),
                submitted_at=_hour_ago(),
            ),
        )
        github = scenario.github

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                github,
                scenario.issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), github.label_history)
        self.assertEqual(
            github.pinned_data(REVIEW_SUMMARY_ISSUE).get("pending_fix_review_summary_max_id"),
            COMMENT_REVIEW_ID,
        )

    def test_approved_body_does_not_resume(self) -> None:
        scenario = self._setup_with_review(
            FakePRReview(
                id=APPROVAL_REVIEW_ID,
                body="LGTM, ship it",
                state="APPROVED",
                user=FakeUser(HUMAN_LOGIN),
                submitted_at=_hour_ago(),
            ),
        )
        github = scenario.github
        scenario.pull_request.approved = True
        scenario.pull_request.approval_head_sha = REVIEWED_SHA

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                github,
                scenario.issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), github.label_history)
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, "done"), github.label_history)
        ping_comments = [body for _, body in github.posted_comments if READY_MESSAGE in body]
        self.assertEqual(len(ping_comments), 1)

    def test_empty_body_review_is_ignored(self) -> None:
        scenario = self._setup_with_review(
            FakePRReview(
                id=EMPTY_REVIEW_ID,
                body="",
                state="CHANGES_REQUESTED",
                user=FakeUser(HUMAN_LOGIN),
                submitted_at=_hour_ago(),
            ),
        )
        github = scenario.github
        scenario.pull_request.changes_requested = True
        scenario.pull_request.changes_requested_head_sha = REVIEWED_SHA

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                github,
                scenario.issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), github.label_history)
