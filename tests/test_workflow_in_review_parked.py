# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for parked-and-closed in_review behavior: awaiting-human parks, manually-closed issues with a still-open PR, and the stale-park-reason clear on the route to fixing."""
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


class AwaitingHumanParkStaysParkedTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """An issue parked awaiting human must stay parked when no new comments
    surface. The handler is manual-merge-only -- there is no auto-recovery
    branch that re-checks the mergeability gate. A human reply (comment or
    relabel) is what unsticks the issue.
    """

    PR_NUMBER = 500
    BRANCH = "orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str, pr_kwargs: dict):
        gh = FakeGitHubClient()
        issue = make_issue(170, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            **pr_kwargs,
        )
        gh.add_pr(pr)
        gh.seed_state(
            170, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            awaiting_human=True,
            park_reason=park_reason,
            # Watermarks past everything visible -- mirrors what
            # _bump_in_review_watermarks set when the original park ran.
            pr_last_comment_id=10_000,
            pr_last_review_comment_id=10_000,
            pr_last_review_summary_id=10_000,
        )
        return gh, issue, pr

    def test_auto_rebase_park_ignores_new_comment_as_fresh_feedback(
        self,
    ) -> None:
        # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
        # `_sync_pr_worktree_to_base`'s retry loop. The human's new
        # comment is the operator's "retry the rebase" signal, NOT
        # fresh PR feedback to route to `fixing`. The handler must
        # stay silent and let the refresh own the comment; otherwise
        # the in_review -> fixing route consumes it as a fix trigger
        # and silently drops the retry intent.
        gh, issue, pr = self._parked_issue(
            park_reason="auto_base_rebase_push_failed",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )
        # Fresh human comment past the watermark.
        gh._issues[170].comments.append(FakeComment(
            id=20_000, body="branch reconciled, please retry",
            user=FakeUser("human"),
        ))

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No fixing route, no relabel, no `pending_fix_*` bookmarks,
        # no PR comment posted.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertEqual(gh.posted_comments, [])
        # Park preserved verbatim so the refresh's next tick still sees
        # the comment + park combo and can drive the retry.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(
            data.get("park_reason"), "auto_base_rebase_push_failed",
        )
        self.assertIsNone(data.get("pending_fix_at"))

    def test_unmergeable_park_stays_parked_when_pr_becomes_mergeable(
        self,
    ) -> None:
        # Even if the PR silently becomes mergeable (rebase resolved a
        # conflict, branch protection dropped), the handler does NOT
        # auto-recover -- the orchestrator never merges from in_review.
        # Park flags stay so the operator notices and drives the merge.
        gh, issue, pr = self._parked_issue(
            park_reason="unmergeable",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")


class ManuallyClosedInReviewIssueTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An open in_review issue closed manually by a human is a stop signal.
    The closed-in_review sweep yields the issue (so a Resolves-#N auto-close
    can finalize to `done`), but if the linked PR is still open the sweep
    has surfaced a manually-closed issue and `_handle_in_review` must mark
    it rejected before the mergeable / HITL-ping path runs.
    """

    PR_NUMBER = 700
    BRANCH = "orchestrator/issue-250"

    def _setup(self, **pr_kwargs):
        gh = FakeGitHubClient()
        issue = make_issue(250, label="in_review")
        issue.closed = True  # human closed the issue, PR still open
        gh.add_issue(issue)
        defaults = dict(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        defaults.update(pr_kwargs)
        pr = FakePR(**defaults)
        gh.add_pr(pr)
        gh.seed_state(
            250, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_manually_closed_with_open_pr_marks_rejected(self) -> None:
        gh, issue, pr = self._setup()

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # The handler must not fall through to the HITL ping over a
        # manually-closed issue even though the PR is otherwise mergeable.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((250, "rejected"), gh.label_history)
        self.assertNotIn((250, "done"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(250))
        self.assertEqual(gh.posted_comments, [])
        # Closing the issue while the PR is still open is a human stop
        # signal. The PR may still be useful for inspection / salvage, so
        # cleanup must NOT delete the branch here -- the operator drives
        # that, or it fires once the PR itself is closed.
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_manually_closed_then_pr_closed_later_requires_manual_cleanup(
        self,
    ) -> None:
        # Documents the known caveat: once the orchestrator flips the
        # closed-issue to `rejected`, the issue falls outside the
        # closed-issue sweep (`list_pollable_issues` only sweeps closed
        # issues still labeled `in_review` / `resolving_conflict`) AND
        # the dispatcher is a no-op for `rejected`. A subsequent PR close
        # is therefore never observed by the orchestrator and the
        # operator must clean up the branch / worktree by hand.
        gh, issue, pr = self._setup()
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertIn((250, "rejected"), gh.label_history)
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Operator now closes the PR. The issue is already closed +
        # rejected, so the polling sweep does not include it on the next
        # tick -- the handler never runs and cleanup never fires.
        pr.state = "closed"
        pollable_numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertNotIn(
            250, pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_manually_closed_does_not_resume_dev_on_new_comments(self) -> None:
        # Even with new PR feedback past the watermark, a manually-closed
        # issue should not spawn a dev fix -- the human closing the issue
        # superseded any open feedback.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup()
        pr.issue_comments.append(
            FakeComment(
                id=2000, body="actually let's reconsider",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((250, "rejected"), gh.label_history)

    def test_external_merge_with_closed_issue_still_finalizes_done(self) -> None:
        # The original closed-issue sweep purpose: a Resolves #N footer
        # auto-closes the issue when the PR merges. Issue closed AND PR
        # merged must still flip to `done`, not `rejected`.
        gh = FakeGitHubClient()
        issue = make_issue(251, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=701, head_branch="orchestrator/issue-251",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(251, pr_number=701, branch="orchestrator/issue-251")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((251, "done"), gh.label_history)
        self.assertNotIn((251, "rejected"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(251))


class StaleParkReasonClearedOnFixingRouteTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A transient in_review park (unmergeable) followed by a fresh PR
    comment must clear the stale `park_reason` and `awaiting_human` flags
    as part of the in_review -> fixing route so the fixing handler is not
    greeted with stale park state.
    """

    PR_NUMBER = 1200
    BRANCH = "orchestrator/issue-700"

    def test_stale_park_reason_cleared_on_route_to_fixing(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Tick 0 already parked for unmergeable; the human posted a
        # follow-up comment ("any update?") to nudge the orchestrator.
        issue = make_issue(700, label="in_review", comments=[
            FakeComment(
                id=3000, body="any update?",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            700,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=2999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Carryover from the original transient park.
            awaiting_human=True,
            park_reason="unmergeable",
        )

        # Tick A: the new comment arrives; the handler routes to `fixing`
        # and clears both the stale `park_reason` and `awaiting_human`
        # flag so the fixing handler is not greeted with stale park state.
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((700, "fixing"), gh.label_history)
        data = gh.pinned_data(700)
        self.assertFalse(
            data.get("awaiting_human"),
            "the route to fixing consumes the human signal and clears the "
            "stale awaiting_human flag",
        )
        self.assertIsNone(
            data.get("park_reason"),
            "stale 'unmergeable' park reason must be cleared by the route "
            "to fixing",
        )
        self.assertEqual(data.get("pending_fix_issue_max_id"), 3000)
        self.assertEqual(gh.merge_calls, [])
