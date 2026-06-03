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

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class InReviewRoutesFreshFeedbackToFixingTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Fresh actionable PR feedback during `in_review` must hand the issue
    off to `fixing` immediately -- no debounce wait, no dev spawn from the
    in_review handler itself. The pending-fix bookmark recorded in pinned
    state gives the (future) fixing handler a starting point for the
    triggering comment.
    """

    PR_NUMBER = 880
    BRANCH = "orchestrator/issue-880"

    def _seed_in_review_with_pr(self, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(880, label="in_review")
        gh.add_issue(issue)
        if pr is None:
            pr = FakePR(
                number=self.PR_NUMBER, head_branch=self.BRANCH,
                head=FakePRRef(sha="cafe1234"),
                mergeable=True, check_state="success",
            )
        gh.add_pr(pr)
        state = dict(
            pr_number=pr.number,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(880, **state)
        return gh, issue, pr

    def test_fresh_pr_conversation_comment_flips_to_fixing_no_dev_spawn(
        self,
    ) -> None:
        # The headline contract: a single fresh PR conversation comment
        # within the debounce window must route the issue from `in_review`
        # to `fixing` on this tick. The dev is NOT spawned by
        # `_handle_in_review` any more -- the fixing stage owns that step.
        # Run through the full dispatcher (`_process_issue`) so the test
        # also covers the routing wiring end-to-end.
        now = datetime.now(timezone.utc)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000,
                    body="please tighten the integration test",
                    user=FakeUser("alice"),
                    created_at=now,  # well inside the debounce window
                ),
            ],
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
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
        self.assertIn((880, "fixing"), gh.label_history)
        # Pending-fix metadata records the triggering comment id and an
        # ISO timestamp so the fixing handler has a bookmark.
        data = gh.pinned_data(880)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 3000)
        self.assertIn("pending_fix_at", data)
        # Watermark stays put so the fixing handler can rescan and reach
        # the triggering comment on its next tick.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)

    def test_no_fresh_feedback_pings_hitl_for_manual_merge(self) -> None:
        # The in_review -> fixing route must NOT preempt the mergeable /
        # HITL-ping path: an approved, mergeable, green PR with no fresh
        # PR comments earns a one-shot HITL ping (the orchestrator never
        # merges) and stays open.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge, no fixing route, no terminal flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertNotIn("pending_fix_at", gh.pinned_data(880))
        # HITL ping fired exactly once.
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(880).get("ready_ping_sha"), "cafe1234",
        )

    def test_no_fresh_feedback_preserves_pr_merged_terminal(self) -> None:
        # Existing terminal PR handling must still finalize the issue to
        # `done` on an external merge -- the fixing route is gated on
        # fresh PR feedback and must not preempt the merged-PR branch.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(880))

    def test_fresh_issue_thread_comment_routes_to_fixing_despite_drift_hash(
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
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh = FakeGitHubClient()
        issue = make_issue(1660, label="in_review")
        # Issue-thread comment posted after the watermark; the hash that
        # was recorded earlier did not include it, so the drift detector
        # WOULD fire on the next tick if the scan order were wrong.
        issue.comments.append(FakeComment(
            id=7000, body="please tighten the docstring",
            user=FakeUser("alice"), created_at=long_ago,
        ))
        gh.add_issue(issue)
        pr = FakePR(
            number=1661, head_branch="orchestrator/issue-1660",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            1660,
            pr_number=pr.number,
            branch="orchestrator/issue-1660",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=6999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Stale hash that doesn't cover the human comment above --
            # the drift path WOULD fire on this tick if the scan order
            # were wrong (this is the reviewer's reproducer).
            user_content_hash="stale-hash-from-before-the-human-comment",
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Contract: no dev spawn, no flip to `validating`.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((1660, "validating"), gh.label_history)
        # The issue routed to `fixing` and recorded the triggering
        # bookmark.
        self.assertIn((1660, "fixing"), gh.label_history)
        data = gh.pinned_data(1660)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 7000)
        self.assertIn("pending_fix_at", data)
        # And the hash was refreshed so the drift path does NOT
        # double-fire on the same comment changes after the fixing
        # handler (or an operator) bounces the issue back to `in_review`.
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-before-the-human-comment",
        )


if __name__ == "__main__":
    unittest.main()
