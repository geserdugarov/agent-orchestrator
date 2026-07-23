# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for parked-and-closed in_review behavior: awaiting-human parks,
manually-closed issues with a still-open PR, and the stale-park-reason clear
on the route to fixing."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config

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
    _agent,
    _issue_branch,
)

PARKED_ISSUE = 170
PARKED_PR = 500
PARK_WATERMARK = 10_000
RETRY_COMMENT_ID = 20_000
CLOSED_ISSUE = 250
CLOSED_ISSUE_PR = 700
CLOSED_ISSUE_WATERMARK = 999
RECONSIDER_COMMENT_ID = 2000
REVIEW_DEBOUNCE_SECONDS = 600
MERGED_ISSUE = 251
MERGED_PR = 701
STALE_PARK_ISSUE = 700
STALE_PARK_PR = 1200
STALE_PARK_COMMENT_ID = 3000
STALE_PARK_WATERMARK = 2999
LABEL_IN_REVIEW = "in_review"
LABEL_REJECTED = "rejected"
REVIEWED_SHA = "cafe1234"
CHECKS_SUCCESS = "success"
RUN_AGENT = "run_agent"


class _ParkedInReviewFixtureMixin(_PatchedWorkflowMixin):
    def _parked_issue(self, *, park_reason: str, pr_kwargs: dict):
        gh = FakeGitHubClient()
        issue = make_issue(PARKED_ISSUE, label=LABEL_IN_REVIEW)
        gh.add_issue(issue)
        pr = FakePR(
            number=PARKED_PR,
            head_branch=_issue_branch(PARKED_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            **pr_kwargs,
        )
        gh.add_pr(pr)
        gh.seed_state(
            PARKED_ISSUE,
            pr_number=PARKED_PR,
            branch=_issue_branch(PARKED_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            park_reason=park_reason,
            # Every scan watermark sits past everything visible, so the
            # fresh-feedback scan surfaces nothing and the parked-tick guard
            # keeps the issue parked -- the post-park state a real tick leaves.
            pr_last_comment_id=PARK_WATERMARK,
            pr_last_review_comment_id=PARK_WATERMARK,
            pr_last_review_summary_id=PARK_WATERMARK,
        )
        return gh, issue, pr


class AwaitingHumanParkStaysParkedTest(
    unittest.TestCase,
    _ParkedInReviewFixtureMixin,
):
    """Keep awaiting-human review issues parked until an operator acts."""

    def test_auto_rebase_park_ignores_new_comment(self) -> None:
        # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
        # `_sync_pr_worktree_to_base`'s retry loop. The human's new
        # comment is the operator's "retry the rebase" signal, NOT
        # fresh PR feedback to route to `fixing`. The handler must
        # stay silent and let the refresh own the comment; otherwise
        # the in_review -> fixing route consumes it as a fix trigger
        # and silently drops the retry intent.
        gh, issue, pr = self._parked_issue(
            park_reason="auto_base_rebase_push_failed",
            pr_kwargs=dict(mergeable=True, check_state=CHECKS_SUCCESS),
        )
        # Fresh human comment past the watermark.
        gh._issues[PARKED_ISSUE].comments.append(
            FakeComment(
                id=RETRY_COMMENT_ID,
                body="branch reconciled, please retry",
                user=FakeUser("human"),
            )
        )

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No fixing route, no relabel, no `pending_fix_*` bookmarks,
        # no PR comment posted.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertEqual(gh.posted_comments, [])
        # Park preserved verbatim so the refresh's next tick still sees
        # the comment + park combo and can drive the retry.
        state = gh.pinned_data(PARKED_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(
            state.get("park_reason"),
            "auto_base_rebase_push_failed",
        )
        self.assertIsNone(state.get("pending_fix_at"))

    def test_mergeable_pr_stays_parked(self) -> None:
        # Even if the PR silently becomes mergeable (rebase resolved a
        # conflict, branch protection dropped), the handler does NOT
        # auto-recover -- the orchestrator never merges from in_review.
        # Park flags stay so the operator notices and drives the merge.
        gh, issue, pr = self._parked_issue(
            park_reason="unmergeable",
            pr_kwargs=dict(mergeable=True, check_state=CHECKS_SUCCESS),
        )

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved.
        state = gh.pinned_data(PARKED_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("park_reason"), "unmergeable")


class _ClosedInReviewFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self, **pr_kwargs):
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_ISSUE, label=LABEL_IN_REVIEW)
        issue.closed = True  # human closed the issue, PR still open
        gh.add_issue(issue)
        defaults = dict(
            number=CLOSED_ISSUE_PR,
            head_branch=_issue_branch(CLOSED_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        defaults.update(pr_kwargs)
        pr = FakePR(**defaults)
        gh.add_pr(pr)
        gh.seed_state(
            CLOSED_ISSUE,
            pr_number=CLOSED_ISSUE_PR,
            branch=_issue_branch(CLOSED_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=CLOSED_ISSUE_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr


class ManuallyClosedInReviewIssueTest(
    unittest.TestCase,
    _ClosedInReviewFixtureMixin,
):
    """Treat a manually closed issue with an open PR as rejected."""

    def test_open_pr_marks_rejected(self) -> None:
        gh, issue, pr = self._setup()

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        # The handler must not fall through to the HITL ping over a
        # manually-closed issue even though the PR is otherwise mergeable.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((CLOSED_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertNotIn((CLOSED_ISSUE, "done"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(CLOSED_ISSUE))
        self.assertEqual(gh.posted_comments, [])
        # Closing the issue while the PR is still open is a human stop
        # signal. The PR may still be useful for inspection / salvage, so
        # cleanup must NOT delete the branch here -- the operator drives
        # that, or it fires once the PR itself is closed.
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_later_pr_close_needs_manual_cleanup(self) -> None:
        # Documents the known caveat: once the orchestrator flips the
        # closed-issue to `rejected`, the issue falls outside the
        # closed-issue sweep (`list_pollable_issues` only sweeps closed
        # issues still labeled `in_review` / `resolving_conflict`) AND
        # the dispatcher is a no-op for `rejected`. A subsequent PR close
        # is therefore never observed by the orchestrator and the
        # operator must clean up the branch / worktree by hand.
        gh, issue, pr = self._setup()
        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        self.assertIn((CLOSED_ISSUE, LABEL_REJECTED), gh.label_history)
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Operator now closes the PR. The issue is already closed +
        # rejected, so the polling sweep does not include it on the next
        # tick -- the handler never runs and cleanup never fires.
        pr.state = "closed"
        pollable_numbers = {pollable.number for pollable in gh.list_pollable_issues()}
        self.assertNotIn(
            CLOSED_ISSUE,
            pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_new_comments_do_not_resume_dev(self) -> None:
        # Even with new PR feedback past the watermark, a manually-closed
        # issue should not spawn a dev fix -- the human closing the issue
        # superseded any open feedback.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup()
        pr.issue_comments.append(
            FakeComment(
                id=RECONSIDER_COMMENT_ID,
                body="actually let's reconsider",
                user=FakeUser("alice"),
                created_at=long_ago,
            ),
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((CLOSED_ISSUE, LABEL_REJECTED), gh.label_history)

    def test_external_merge_finalizes_done(self) -> None:
        # The original closed-issue sweep purpose: a Resolves #N footer
        # auto-closes the issue when the PR merges. Issue closed AND PR
        # merged must still flip to `done`, not `rejected`.
        gh = FakeGitHubClient()
        issue = make_issue(MERGED_ISSUE, label=LABEL_IN_REVIEW)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=MERGED_PR,
            head_branch=_issue_branch(MERGED_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(MERGED_ISSUE, pr_number=MERGED_PR, branch=_issue_branch(MERGED_ISSUE))

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertIn((MERGED_ISSUE, "done"), gh.label_history)
        self.assertNotIn((MERGED_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(MERGED_ISSUE))


class StaleParkReasonClearedOnFixingRouteTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A transient in_review park (unmergeable) followed by a fresh PR
    comment must clear the stale `park_reason` and `awaiting_human` flags
    as part of the in_review -> fixing route so the fixing handler is not
    greeted with stale park state.
    """

    def test_fixing_route_clears_stale_reason(self) -> None:
        gh = FakeGitHubClient()
        # Tick 0 already parked for unmergeable; the human posted a
        # follow-up comment ("any update?") to nudge the orchestrator.
        issue = make_issue(
            STALE_PARK_ISSUE,
            label=LABEL_IN_REVIEW,
            comments=[
                FakeComment(
                    id=STALE_PARK_COMMENT_ID,
                    body="any update?",
                    user=FakeUser("alice"),
                    created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=STALE_PARK_PR,
            head_branch=_issue_branch(STALE_PARK_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        gh.add_pr(pr)
        gh.seed_state(
            STALE_PARK_ISSUE,
            pr_number=STALE_PARK_PR,
            branch=_issue_branch(STALE_PARK_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=STALE_PARK_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Carryover from the original transient park.
            awaiting_human=True,
            park_reason="unmergeable",
        )

        # Tick A: the new comment arrives; the handler routes to `fixing`
        # and clears both the stale `park_reason` and `awaiting_human`
        # flag so the fixing handler is not greeted with stale park state.
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((STALE_PARK_ISSUE, "fixing"), gh.label_history)
        state = gh.pinned_data(STALE_PARK_ISSUE)
        self.assertFalse(
            state.get("awaiting_human"),
            "the route to fixing consumes the human signal and clears the stale awaiting_human flag",
        )
        self.assertIsNone(
            state.get("park_reason"),
            "stale 'unmergeable' park reason must be cleared by the route to fixing",
        )
        self.assertEqual(state.get("pending_fix_issue_max_id"), STALE_PARK_COMMENT_ID)
        self.assertEqual(gh.merge_calls, [])
