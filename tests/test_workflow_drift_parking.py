# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift clarification parking tests."""
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests import workflow_drift_test_support as support


class DriftNonAckResponseParksTest(
    unittest.TestCase, support._PatchedWorkflowMixin,
):
    """A non-empty no-commit response WITHOUT the `ACK:` marker -- e.g.
    a clarification question -- must park awaiting human, not silently
    advance the workflow with a misleading "satisfies" comment."""

    def test_validating_clarification_parks(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._VALIDATING_CLARIFICATION_ISSUE_NUMBER,
            label=support.LABEL_VALIDATING,
            body=support.CLARIFIED_BODY,
        )
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._VALIDATING_CLARIFICATION_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-601",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._VALIDATING_CLARIFICATION_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            review_round=1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-601",
        )

        self._run(
            lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION,
                last_message=(
                    "Should the empty-input case also raise, or return "
                    "an empty list? Need clarification."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=[support.SAME_SHA, support.SAME_SHA],
        )

        state = gh.pinned_data(support._VALIDATING_CLARIFICATION_ISSUE_NUMBER)
        # Must park awaiting human so the real question isn't lost.
        self.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
        # Must NOT have posted the misleading "satisfies" comment.
        self.assertFalse(any(
            support.EXISTING_WORK_MESSAGE in body
            for _, body in gh.posted_comments
        ))
        # The question text was surfaced via `_on_question`.
        self.assertTrue(any(
            "Should the empty-input case" in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_clarification_parks(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._IN_REVIEW_CLARIFICATION_ISSUE_NUMBER,
            label=support.LABEL_IN_REVIEW,
            body=support.CLARIFIED_BODY,
        )
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._IN_REVIEW_CLARIFICATION_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-701",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._IN_REVIEW_CLARIFICATION_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-701",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION,
                last_message=(
                    "Does the updated body imply I should also rename "
                    "`old_fn`? Please confirm."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=[support.UNCHANGED_SHA, support.UNCHANGED_SHA],
        )

        state = gh.pinned_data(support._IN_REVIEW_CLARIFICATION_ISSUE_NUMBER)
        # Park flagged.
        self.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
        # NOT bounced to validating: the dev didn't ack OR commit, so
        # the in_review label is preserved and the human resolves the
        # question.
        self.assertNotIn(
            (support._IN_REVIEW_CLARIFICATION_ISSUE_NUMBER, support.LABEL_VALIDATING),
            gh.label_history,
        )
        # Misleading "satisfies" comment NOT posted.
        self.assertFalse(any(
            support.EXISTING_WORK_MESSAGE in body
            for _, body in gh.posted_comments
        ))

    def test_implementing_clarification_parks(
        self,
    ) -> None:
        # The implementing-stage inline drift handler shares the same
        # contract: non-empty + no-commit + no ACK -> park as question.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._IMPLEMENTING_CLARIFICATION_ISSUE_NUMBER,
            label=support.LABEL_IMPLEMENTING,
            body="updated requirements",
        )
        gh.add_issue(issue)
        gh.seed_state(
            support._IMPLEMENTING_CLARIFICATION_ISSUE_NUMBER,
            user_content_hash=support.STALE_HASH,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            awaiting_human=False,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-602",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION,
                last_message=(
                    "I'd like to clarify: should the schema migration "
                    "run forward-only or also support rollback?"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["sha-before", "sha-before"],
        )

        state = gh.pinned_data(support._IMPLEMENTING_CLARIFICATION_ISSUE_NUMBER)
        self.assertTrue(state.get(support.KEY_AWAITING_HUMAN))
        self.assertFalse(any(
            support.EXISTING_WORK_MESSAGE in body
            for _, body in gh.posted_comments
        ))
        # The dev's question was surfaced.
        self.assertTrue(any(
            "schema migration" in body
            for _, body in gh.posted_comments
        ))
