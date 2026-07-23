# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fresh-feedback routing from `in_review` to `fixing`: the in_review
handler must hand the issue off to `fixing` immediately when fresh
actionable PR feedback lands (no debounce wait, no dev spawn), record a
`pending_fix_*` bookmark, and preserve the `pr_last_*` watermarks so the
fixing rescan reaches the triggering comment. The mergeable / HITL-ping
path and the merged-PR terminal must still win when there is no fresh
feedback. The drift-hash regression also lives here: a stale
`user_content_hash` covering a fresh issue-thread comment must not
trigger a `validating` flip ahead of the fresh-feedback scan."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    LABEL_FIXING,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

FRESH_FEEDBACK_ISSUE = 880
FRESH_FEEDBACK_PR = 880
FRESH_FEEDBACK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-880"
FEEDBACK_WATERMARK = 1999
PR_FEEDBACK_ID = 3000
REVIEW_DEBOUNCE_SECONDS = 600
FIRST_PR_COMMENT_ID = 2100
SECOND_PR_COMMENT_ID = 2200
FIRST_INLINE_COMMENT_ID = 40
SECOND_INLINE_COMMENT_ID = 41
ISSUE_COMMENT_ID = 2050
DRIFT_FEEDBACK_ISSUE = 1660
DRIFT_FEEDBACK_PR = 1661
DRIFT_FEEDBACK_ID = 7000
DRIFT_FEEDBACK_WATERMARK = 6999
REVIEWED_SHA = "cafe1234"
CHECKS_SUCCESS = "success"
HUMAN_LOGIN = "alice"


class _FreshFeedbackFixtureMixin(_PatchedWorkflowMixin):
    def _seed_in_review_with_pr(self, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_FEEDBACK_ISSUE, label="in_review")
        gh.add_issue(issue)
        if pr is None:
            pr = FakePR(
                number=FRESH_FEEDBACK_PR,
                head_branch=FRESH_FEEDBACK_BRANCH,
                head=FakePRRef(sha=REVIEWED_SHA),
                mergeable=True,
                check_state=CHECKS_SUCCESS,
            )
        gh.add_pr(pr)
        seed_state = dict(
            pr_number=pr.number,
            branch=FRESH_FEEDBACK_BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=FEEDBACK_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        if extra_state:
            seed_state.update(extra_state)
        gh.seed_state(FRESH_FEEDBACK_ISSUE, **seed_state)
        return gh, issue, pr

    def _assert_surface_bookmarks(self, github, state) -> None:
        self.assertIn((FRESH_FEEDBACK_ISSUE, LABEL_FIXING), github.label_history)
        self.assertEqual(
            state.get("pending_fix_issue_ids"),
            [ISSUE_COMMENT_ID, FIRST_PR_COMMENT_ID, SECOND_PR_COMMENT_ID],
        )
        self.assertEqual(state.get("pending_fix_issue_max_id"), SECOND_PR_COMMENT_ID)
        self.assertEqual(state.get("pending_fix_review_ids"), [FIRST_INLINE_COMMENT_ID, SECOND_INLINE_COMMENT_ID])
        self.assertEqual(state.get("pending_fix_review_max_id"), SECOND_INLINE_COMMENT_ID)
        self.assertEqual(state.get("pending_fix_review_summary_ids"), [7])
        self.assertEqual(state.get("pending_fix_review_summary_max_id"), 7)
        self.assertEqual(state.get("pr_last_comment_id"), FEEDBACK_WATERMARK)

    def _seed_drift_feedback(self):
        github = FakeGitHubClient()
        issue = make_issue(DRIFT_FEEDBACK_ISSUE, label="in_review")
        issue.comments.append(
            FakeComment(
                id=DRIFT_FEEDBACK_ID,
                body="please tighten the docstring",
                user=FakeUser(HUMAN_LOGIN),
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
        )
        github.add_issue(issue)
        github.add_pr(
            FakePR(
                number=DRIFT_FEEDBACK_PR,
                head_branch=_issue_branch(DRIFT_FEEDBACK_ISSUE),
                head=FakePRRef(sha=REVIEWED_SHA),
                mergeable=True,
                check_state=CHECKS_SUCCESS,
            ),
        )
        github.seed_state(
            DRIFT_FEEDBACK_ISSUE,
            pr_number=DRIFT_FEEDBACK_PR,
            branch=_issue_branch(DRIFT_FEEDBACK_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=DRIFT_FEEDBACK_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            user_content_hash="stale-hash-from-before-the-human-comment",
        )
        return github, issue


class InReviewRoutesFreshFeedbackToFixingTest(
    unittest.TestCase,
    _FreshFeedbackFixtureMixin,
):
    """Route fresh review feedback to fixing without spawning the dev."""

    def test_pr_comment_routes_without_dev_spawn(
        self,
    ) -> None:
        # The headline contract: a single fresh PR conversation comment
        # within the debounce window must route the issue from `in_review`
        # to `fixing` on this tick. The dev is NOT spawned by
        # `_handle_in_review` any more -- the fixing stage owns that step.
        # Run through the full dispatcher (`_process_issue`) so the test
        # also covers the routing wiring end-to-end.
        pr = FakePR(
            number=FRESH_FEEDBACK_PR,
            head_branch=FRESH_FEEDBACK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=PR_FEEDBACK_ID,
                    body="please tighten the integration test",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=datetime.now(timezone.utc),
                ),
            ],
        )
        gh, issue = self._seed_in_review_with_pr(pr=pr)[:2]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run(
                lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev spawn during the debounce window (or after it -- the
        # in_review handler no longer spawns the dev at all).
        mocks["run_agent"].assert_not_called()
        # No merge attempt either: the orchestrator never merges and
        # the fresh feedback routes to fixing.
        self.assertEqual(gh.merge_calls, [])
        # The label flipped to `fixing` this tick.
        self.assertIn((FRESH_FEEDBACK_ISSUE, LABEL_FIXING), gh.label_history)
        # Pending-fix metadata records the triggering comment id and an
        # ISO timestamp so the fixing handler has a bookmark.
        pinned_state = gh.pinned_data(FRESH_FEEDBACK_ISSUE)
        self.assertEqual(pinned_state.get("pending_fix_issue_max_id"), PR_FEEDBACK_ID)
        self.assertIn("pending_fix_at", pinned_state)
        # Watermark stays put so the fixing handler can rescan and reach
        # the triggering comment on its next tick.
        self.assertEqual(pinned_state.get("pr_last_comment_id"), FEEDBACK_WATERMARK)

    def test_route_persists_ids_for_all_surfaces(self) -> None:
        # The route must persist the FULL per-surface id lists (not just the
        # max ids) so a later fixing tick can reconstruct the exact batch
        # after the watermarks advance past it. Seed feedback that spans all
        # three id namespaces with more than one item per surface so a
        # max-only bookmark would lose the lower members.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=FRESH_FEEDBACK_PR,
            head_branch=FRESH_FEEDBACK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=FIRST_PR_COMMENT_ID,
                    body="pr conv one",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=old,
                ),
                FakeComment(
                    id=SECOND_PR_COMMENT_ID,
                    body="pr conv two",
                    user=FakeUser("bob"),
                    created_at=old,
                ),
            ],
            review_comments=[
                FakeComment(
                    id=FIRST_INLINE_COMMENT_ID,
                    body="inline one",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=old,
                ),
                FakeComment(
                    id=SECOND_INLINE_COMMENT_ID,
                    body="inline two",
                    user=FakeUser("bob"),
                    created_at=old,
                ),
            ],
            reviews=[
                FakePRReview(
                    id=7,
                    body="changes please",
                    state="CHANGES_REQUESTED",
                    submitted_at=old,
                ),
            ],
        )
        gh, issue = self._seed_in_review_with_pr(pr=pr)[:2]
        # Also seed a fresh issue-thread comment on the issue itself so the
        # issue-space id list mixes issue-thread and PR-conversation ids.
        issue.comments.append(
            FakeComment(
                id=ISSUE_COMMENT_ID,
                body="issue thread note",
                user=FakeUser("carol"),
                created_at=old,
            )
        )

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        state = gh.pinned_data(FRESH_FEEDBACK_ISSUE)
        self._assert_surface_bookmarks(gh, state)

    def test_no_feedback_pings_for_manual_merge(self) -> None:
        # The in_review -> fixing route must NOT preempt the mergeable /
        # HITL-ping path: an approved, mergeable, green PR with no fresh
        # PR comments earns a one-shot HITL ping (the orchestrator never
        # merges) and stays open.
        pr = FakePR(
            number=FRESH_FEEDBACK_PR,
            head_branch=FRESH_FEEDBACK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            approved=True,
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No merge, no fixing route, no terminal flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((FRESH_FEEDBACK_ISSUE, "done"), gh.label_history)
        self.assertNotIn((FRESH_FEEDBACK_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertNotIn("pending_fix_at", gh.pinned_data(FRESH_FEEDBACK_ISSUE))
        # HITL ping fired exactly once.
        ping_comments = [body for _, body in gh.posted_comments if "ready for review/merge" in body]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(FRESH_FEEDBACK_ISSUE).get("ready_ping_sha"),
            REVIEWED_SHA,
        )

    def test_no_feedback_keeps_merged_terminal(self) -> None:
        # Existing terminal PR handling must still finalize the issue to
        # `done` on an external merge -- the fixing route is gated on
        # fresh PR feedback and must not preempt the merged-PR branch.
        pr = FakePR(
            number=FRESH_FEEDBACK_PR,
            head_branch=FRESH_FEEDBACK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            merged=True,
            state="closed",
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertIn((FRESH_FEEDBACK_ISSUE, "done"), gh.label_history)
        self.assertNotIn((FRESH_FEEDBACK_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(FRESH_FEEDBACK_ISSUE))

    def test_issue_comment_wins_over_drift(
        self,
    ) -> None:
        # Regression test for the reviewer's reproducer: a normal fresh
        # issue-thread review comment used to trigger user-content drift
        # (because `user_content_hash` covers human issue comments) and
        # the drift path would `_resume_dev_with_text` + flip to
        # `validating` -- violating the contract that any fresh issue-
        # thread feedback during `in_review` records `pending_fix_*` and
        # routes to `fixing`. Seed a stale prior `user_content_hash` so
        # the drift path WOULD fire if the ordering were wrong, then
        # confirm the fresh-feedback scan wins.
        gh, issue = self._seed_drift_feedback()

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # Contract: no dev spawn, no flip to `validating`.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((DRIFT_FEEDBACK_ISSUE, "validating"), gh.label_history)
        # The issue routed to `fixing` and recorded the triggering
        # bookmark.
        self.assertIn((DRIFT_FEEDBACK_ISSUE, LABEL_FIXING), gh.label_history)
        pinned_state = gh.pinned_data(DRIFT_FEEDBACK_ISSUE)
        self.assertEqual(pinned_state.get("pending_fix_issue_max_id"), DRIFT_FEEDBACK_ID)
        self.assertIn("pending_fix_at", pinned_state)
        # And the hash was refreshed so the drift path does NOT
        # double-fire on the same comment changes after the fixing
        # handler (or an operator) bounces the issue back to `in_review`.
        self.assertNotEqual(
            pinned_state.get("user_content_hash"),
            "stale-hash-from-before-the-human-comment",
        )


if __name__ == "__main__":
    unittest.main()
