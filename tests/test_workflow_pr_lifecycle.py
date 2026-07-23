# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Review-verdict and awaiting-human lifecycle event tests."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config, workflow

from tests import workflow_pr_lifecycle_test_support as support


class ReviewVerdictEventEmissionTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """`_handle_validating` emits a `review_verdict` event after parsing the
    reviewer agent's final message, so an operator tailing the JSONL sink
    sees approve/changes-requested decisions inline with the rest of the
    workflow trace.
    """

    def test_approved_verdict_emits_event(self) -> None:
        gh, issue, pr, last = support._seeded_verdict(support.REVIEW_APPROVED_MESSAGE)
        support._run_verdict(self, gh, issue, pr, last)
        verdict = support._only_event(gh, support.EVENT_REVIEW_VERDICT)
        self.assertEqual(verdict[support.KEY_VERDICT], support.VERDICT_APPROVED)
        self.assertEqual(verdict[support.KEY_STAGE], support.LABEL_VALIDATING)
        self.assertEqual(verdict[support.KEY_REVIEW_ROUND], 0)
        self.assertEqual(verdict[support.KEY_PR_NUMBER], support._VERDICT_PR_NUMBER)
        self.assertEqual(verdict["session_id"], "sess-review")

    def test_changes_requested_verdict_emits_event(self) -> None:
        gh, issue, pr, last = support._seeded_verdict(
            "1. Add a test\n\nVERDICT: CHANGES_REQUESTED",
        )
        support._run_verdict(self, gh, issue, pr, last)
        verdict = support._only_event(gh, support.EVENT_REVIEW_VERDICT)
        self.assertEqual(verdict[support.KEY_VERDICT], support.VERDICT_CHANGES_REQUESTED)

    def test_unknown_verdict_emits_event(self) -> None:
        gh, issue, pr, last = support._seeded_verdict("no marker here")
        support._run_verdict(self, gh, issue, pr, last)
        verdict = support._only_event(gh, support.EVENT_REVIEW_VERDICT)
        self.assertEqual(verdict[support.KEY_VERDICT], support.VERDICT_UNKNOWN)


class ParkAwaitingHumanEventEmissionTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """Every park path (the shared `_park_awaiting_human` helper plus the
    inline `_on_question` / `_on_dirty_worktree` helpers) emits a
    `park_awaiting_human` event tagged with the current stage and an
    optional `reason` so the JSONL sink mirrors the durable `park_reason`
    field for the operator.
    """

    def test_question_park_has_reason_and_stage(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(6, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(last_message="please clarify the scope"),
            has_new_commits=False,
        )
        park = support._only_event(gh, support.EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(park[support.KEY_STAGE], support.LABEL_IMPLEMENTING)
        self.assertEqual(park[support.KEY_REASON], "agent_question")

    def test_agent_silent_park_carries_reason(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(7, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        park = support._only_event(gh, support.EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(park[support.KEY_REASON], "agent_silent")

    def test_reviewer_timeout_park_carries_reason(self) -> None:
        # Reviewer agent timeout during validating routes through
        # `_park_awaiting_human(reason="reviewer_timeout")` directly.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(8, label=support.LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(8, pr_number=support._TIMEOUT_PR_NUMBER, review_round=1)
        pr = support.FakePR(
            number=support._TIMEOUT_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-8",
            base_branch=support.TEST_BASE_BRANCH, mergeable=True, check_state=support.CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        with patch.object(
            workflow, support.LATEST_PR_COMMENT_IDS, return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(timed_out=True, last_message=""),
                head_shas=[pr.head.sha],
            )
        park = support._only_event(gh, support.EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(park[support.KEY_STAGE], support.LABEL_VALIDATING)
        self.assertEqual(park[support.KEY_REASON], "reviewer_timeout")

    def test_review_cap_park_has_reason(self) -> None:
        # `_handle_validating`'s review-cap exhaustion calls
        # `_park_awaiting_human(reason="review_cap")` directly -- a pure
        # shared-helper park path (no transient `state.set("park_reason",
        # ...)` follow-up like the timeout sites have). The emitted event
        # must still carry the reason.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(10, label=support.LABEL_VALIDATING)
        gh.add_issue(issue)
        # Seed review_round at the cap so the very first tick parks.
        gh.seed_state(
            10,
            pr_number=support._REVIEW_CAP_PR_NUMBER,
            review_round=config.MAX_REVIEW_ROUNDS,
        )
        pr = support.FakePR(
            number=support._REVIEW_CAP_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-10",
            base_branch=support.TEST_BASE_BRANCH, mergeable=True, check_state=support.CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        self._run(
            lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(last_message="should not run"),
        )
        park = support._only_event(gh, support.EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(park[support.KEY_STAGE], support.LABEL_VALIDATING)
        self.assertEqual(park[support.KEY_REASON], "review_cap")

    def test_push_failed_in_on_commits_carries_reason(self) -> None:
        # `_on_commits` is reached via `_handle_implementing` after the
        # agent committed; a failing push routes through
        # `_park_awaiting_human(reason="push_failed")`. Representative
        # test for a helper-only park outside the validating handler.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(support._PUSH_FAILED_ISSUE_NUMBER, label=support.LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(session_id="sess-x", last_message="done"),
            has_new_commits=True,
            push_branch=False,  # simulate push failure
        )
        park = support._only_event(gh, support.EVENT_PARK_AWAITING_HUMAN)
        self.assertEqual(park[support.KEY_STAGE], support.LABEL_IMPLEMENTING)
        self.assertEqual(park[support.KEY_REASON], "push_failed")

    def test_no_park_event_when_run_does_not_park(self) -> None:
        # A clean approval run flips to in_review without parking; no
        # `park_awaiting_human` event should be recorded.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(9, label=support.LABEL_VALIDATING)
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._APPROVAL_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-9",
            base_branch=support.TEST_BASE_BRANCH, mergeable=True, check_state=support.CHECK_SUCCESS,
        )
        gh.add_pr(pr)
        gh.seed_state(9, pr_number=support._APPROVAL_PR_NUMBER, review_round=0)
        with patch.object(
            workflow, support.LATEST_PR_COMMENT_IDS, return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="sess-r", last_message="ok\n\nVERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        self.assertEqual(support._events_of(gh, support.EVENT_PARK_AWAITING_HUMAN), [])
