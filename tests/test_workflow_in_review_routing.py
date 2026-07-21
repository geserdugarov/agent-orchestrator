# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the core in_review routing: merged / closed-not-merged PRs,
HITL ready-ping gates, PR-comment debounce, and the PR-review-summary
surface."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config, workflow, workflow_messages

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
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

ROUTING_ISSUE = 30
ROUTING_PR = 77
CONCURRENT_COMMENT_WATERMARK = 1500
CONCURRENT_COMMENT_ID = 1600
FEEDBACK_COMMENT_ID = 2000
FEEDBACK_WATERMARK = 1999
REVIEW_DEBOUNCE_SECONDS = 600
MERGED_ISSUE = 40
MERGED_PR = 99
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
READY_PING_SHA = "ready_ping_sha"
AWAITING_HUMAN = "awaiting_human"
PR_LAST_COMMENT_ID = "pr_last_comment_id"
HUMAN_LOGIN = "alice"
LABEL_FIXING = "fixing"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"


class _PostWithConcurrentComment:
    def __init__(self, comment):
        self.comment = comment

    def __call__(self, gh, issue, state, body):
        if READY_MESSAGE in body:
            issue.comments.append(self.comment)
        return workflow_messages._post_issue_comment(gh, issue, state, body)


class _InReviewRoutingFixtureMixin(_PatchedWorkflowMixin):
    def _seed(
        self,
        *,
        issue_number: int = ROUTING_ISSUE,
        pr=None,
        with_pr_number: bool = True,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="in_review")
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state: dict = {
            "branch": _issue_branch(ROUTING_ISSUE),
            "dev_agent": "claude",
            "dev_session_id": "dev-sess",
            "review_round": 1,
        }
        if with_pr_number and pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=ROUTING_PR,
            head_branch=_issue_branch(ROUTING_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)


class HandleInReviewTest(
    unittest.TestCase,
    _InReviewRoutingFixtureMixin,
):
    """Route terminal pull-request states from in-review."""

    def test_in_review_pr_merged_externally(self) -> None:
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertIn((ROUTING_ISSUE, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(ROUTING_ISSUE))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # Branch cleanup must fire for an external merge: the PR is gone, so
        # the per-issue worktree and the local + remote branches are dead
        # weight that should not survive past the `done` flip.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh,
            _TEST_SPEC,
            ROUTING_ISSUE,
            branch=_issue_branch(ROUTING_ISSUE),
        )

    def test_in_review_pr_closed_unmerged(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertIn((ROUTING_ISSUE, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(ROUTING_ISSUE))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # The PR is gone, so the orchestrator-owned branch and worktree
        # are dead weight regardless of whether the PR merged or was
        # declined. Cleanup must fire on the rejected terminal too.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh,
            _TEST_SPEC,
            ROUTING_ISSUE,
            branch=_issue_branch(ROUTING_ISSUE),
        )


class InReviewReadyPingRoutingTest(
    unittest.TestCase,
    _InReviewRoutingFixtureMixin,
):
    """Gate and deduplicate the ready-for-merge notification."""

    def test_mergeable_final_docs_ping_human(self) -> None:
        # PR mergeable: post a one-shot HITL ping so the human knows the
        # PR is ready, but stay open (no merge, no label flip, no
        # awaiting_human). The orchestrator is manual-merge-only -- it
        # never calls `gh.merge_pr` from in_review. The ping must mention
        # every HITL handle so notifications fire even when the reviewer
        # agent approved via comments rather than a formal GitHub review.
        pr = self._open_pr(approved=False, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": REVIEWED_SHA,
                "docs_verdict": "no_change",
            },
        )

        with patch.object(config, "HITL_MENTIONS", "@alice @bob"):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        # Exactly one ping was posted on the issue thread.
        ping_comments = [body for _, body in gh.posted_comments if READY_MESSAGE in body]
        self.assertEqual(len(ping_comments), 1)
        self.assertIn("@alice", ping_comments[0])
        self.assertIn("@bob", ping_comments[0])
        self.assertIn(f"PR #{ROUTING_PR}", ping_comments[0])
        state = gh.pinned_data(ROUTING_ISSUE)
        # De-dup key is the head SHA we pinged for.
        self.assertEqual(state.get(READY_PING_SHA), REVIEWED_SHA)
        # Not parked: subsequent ticks must still react to comments / state.
        self.assertFalse(state.get(AWAITING_HUMAN))
        # Ping is recorded in orchestrator_comment_ids so the next tick's
        # `comments_after` filter excludes it as bot noise without needing
        # the watermark to move (which would risk swallowing a human
        # comment that landed between the earlier scan and the ping).
        ping_id = gh.latest_comment_id(issue)
        self.assertIsNotNone(ping_id)
        self.assertIn(ping_id, state.get("orchestrator_comment_ids", []))

    def test_no_approval_does_not_ping(self) -> None:
        # The ping advertises the PR as ready for review/merge; firing it
        # on a mergeable PR with neither a current final-docs handoff nor
        # a formal GitHub approval would invite a manual merge over a
        # commit no reviewer has signed off on.
        pr = self._open_pr(approved=False, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertIsNone(state.get(READY_PING_SHA))
        self.assertFalse(state.get(AWAITING_HUMAN))

    def test_changes_requested_does_not_ping(self) -> None:
        # A standing human CHANGES_REQUESTED on the current head vetoes
        # the ping; the orchestrator must not advertise the PR as ready
        # while a human review is asking for changes, even when the
        # agent-approved final-docs handoff matches the current head.
        pr = self._open_pr(
            approved=False,
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            changes_requested=True,
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": REVIEWED_SHA,
                "docs_verdict": "updated",
            },
        )

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertIsNone(state.get(READY_PING_SHA))

    def test_in_review_mergeable_dedups_same_head(self) -> None:
        # Second tick on the same head SHA must NOT re-ping; the ping is
        # one-shot per head so a long-lived ready-for-merge PR doesn't spam
        # the HITL handles on every poll.
        pr = self._open_pr(approved=True, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        comments_after_first = list(gh.posted_comments)
        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertEqual(gh.posted_comments, comments_after_first)

    def test_in_review_mergeable_repings_new_head(self) -> None:
        # A new commit on the PR branch shifts pr.head.sha; the ping is
        # keyed on the SHA we last pinged for, so the next tick must
        # re-ping on the new head.
        pr = self._open_pr(approved=True, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        pings_first = [body for _, body in gh.posted_comments if READY_MESSAGE in body]
        pr.head = FakePRRef(sha="beefcafe")
        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        pings_total = [body for _, body in gh.posted_comments if READY_MESSAGE in body]
        self.assertEqual(len(pings_first), 1)
        self.assertEqual(len(pings_total), 2)
        self.assertEqual(gh.pinned_data(ROUTING_ISSUE).get(READY_PING_SHA), "beefcafe")

    def test_stale_final_docs_do_not_ping_new_head(self) -> None:
        # The final-docs marker is a head-SHA approval signal. If another
        # commit lands after documenting, the old marker must not ping the
        # new head; the issue needs another validating/documenting pass.
        pr = self._open_pr(approved=False, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": REVIEWED_SHA,
                "docs_verdict": "no_change",
                READY_PING_SHA: REVIEWED_SHA,
            },
        )
        pr.head = FakePRRef(sha="beefcafe")

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.pinned_data(ROUTING_ISSUE).get(READY_PING_SHA), REVIEWED_SHA)

    def test_ready_ping_keeps_concurrent_human(self) -> None:
        # Race window: a human posts an issue comment AFTER the handler's
        # comment scan but BEFORE the ready-for-merge ping. The ping must
        # NOT bump `pr_last_comment_id` past the unseen human comment;
        # otherwise the next tick's `comments_after` would skip the human
        # feedback and the dev would never resume on it.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(approved=True, mergeable=True, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr, extra_state={PR_LAST_COMMENT_ID: CONCURRENT_COMMENT_WATERMARK})
        # Pre-seed the human comment with an id ABOVE the watermark but
        # BELOW the ping id (the fake comment-id counter starts at 1000,
        # so the next id allocated by `_post_issue_comment` will be the
        # one after this). We splice the comment in mid-handler via a
        # patch on `_post_issue_comment` so it lands AFTER the scan.
        human_comment = FakeComment(
            id=CONCURRENT_COMMENT_ID,
            body="please hold off, doing one more pass",
            user=FakeUser(HUMAN_LOGIN),
            created_at=long_ago,
        )
        with patch.object(
            workflow,
            "_post_issue_comment",
            _PostWithConcurrentComment(human_comment),
        ):
            self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # Watermark must NOT have advanced past the human comment.
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertLess(state.get(PR_LAST_COMMENT_ID), human_comment.id)

        # Second tick: the human comment surfaces. The fresh-feedback
        # scan now runs BEFORE the drift check, so the human comment
        # routes the issue to `fixing` (the dev is not spawned by
        # `_handle_in_review` here). The ping itself is filtered as
        # orchestrator-authored, so the route is driven by the (real,
        # human-authored) `human_comment`.
        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((ROUTING_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(ROUTING_ISSUE).get("pending_fix_issue_max_id"),
            human_comment.id,
        )


class InReviewFeedbackRoutingTest(
    unittest.TestCase,
    _InReviewRoutingFixtureMixin,
):
    """Park or route open pull requests based on checks and feedback."""

    def test_in_review_unmergeable_parks_for_human(self) -> None:
        # PR not mergeable: park awaiting human with
        # `park_reason="unmergeable"`. The orchestrator never routes from
        # in_review to `resolving_conflict`; the human drives the merge
        # (or relabels manually).
        pr = self._open_pr(approved=True, mergeable=False, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr)

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        # Must NOT route to resolving_conflict.
        self.assertNotIn((ROUTING_ISSUE, "resolving_conflict"), gh.label_history)
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get("park_reason"), "unmergeable")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("not mergeable", last_comment)
        # No conflict_round seeded -- the orchestrator never enters the
        # auto-resolution route from here.
        self.assertNotIn("conflict_round", state)

    def test_in_review_mergeable_pending(self) -> None:
        # mergeable=None means GitHub is still computing. Don't ping,
        # don't park; the next tick re-checks once GitHub has decided.
        pr = self._open_pr(approved=True, mergeable=None, check_state=CHECKS_SUCCESS)
        gh, issue = self._seed(pr=pr)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(ROUTING_ISSUE).get(AWAITING_HUMAN))

    def test_inside_debounce_comment_enters_fixing(self) -> None:
        # Fresh PR feedback inside the debounce window must NOT silently
        # wait or spawn the dev: the handler records pending-fix metadata
        # and flips the label to `fixing` immediately so the fixing handler
        # can own its own debounce / resume cycle.
        now = datetime.now(timezone.utc)
        pr = self._open_pr(
            approved=True,
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=FEEDBACK_COMMENT_ID,
                    body="please tighten the docstring",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=now,
                ),
            ],
        )
        # Watermark just below the comment so it surfaces as fresh feedback.
        # An unset watermark would trip the legacy in_review migration and
        # mask this comment as already-consumed.
        gh, issue = self._seed(pr=pr, extra_state={PR_LAST_COMMENT_ID: FEEDBACK_WATERMARK})

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # No dev spawn, no merge attempt (the in_review handler is not the
        # one that drives the fix any more); label flipped to `fixing`.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((ROUTING_ISSUE, LABEL_FIXING), gh.label_history)
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertIn("pending_fix_at", state)
        self.assertEqual(state.get("pending_fix_issue_max_id"), FEEDBACK_COMMENT_ID)
        # Watermarks deliberately NOT bumped: the fixing handler needs the
        # triggering comments to build its dev-resume prompt.
        self.assertEqual(state.get(PR_LAST_COMMENT_ID), FEEDBACK_WATERMARK)

    def test_past_debounce_comment_enters_fixing(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(
            issue_comments=[
                FakeComment(
                    id=FEEDBACK_COMMENT_ID,
                    body="rename foo to bar",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh, issue = self._seed(pr=pr, extra_state={PR_LAST_COMMENT_ID: FEEDBACK_WATERMARK})

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        # Past-debounce feedback also hands off to the fixing stage rather
        # than spawning the dev inline. The fixing handler owns the
        # resume / push / hand-back-to-`validating` cycle (a pushed fix
        # flips DIRECTLY back to `validating` for the reviewer to
        # re-evaluate; docs do not run here, the single docs pass runs
        # after reviewer approval before `in_review`).
        mocks[RUN_AGENT].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((ROUTING_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertNotIn((ROUTING_ISSUE, "validating"), gh.label_history)
        state = gh.pinned_data(ROUTING_ISSUE)
        self.assertIn("pending_fix_at", state)
        self.assertEqual(state.get("pending_fix_issue_max_id"), FEEDBACK_COMMENT_ID)

    def test_in_review_pr_number_missing(self) -> None:
        # Manually-relabeled in_review without a pinned PR -- park once.
        gh, issue = self._seed(pr=None, with_pr_number=False)

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        self.assertTrue(gh.pinned_data(ROUTING_ISSUE).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("without a pinned `pr_number`", last_comment)

        # A second tick with awaiting_human set must NOT re-park (no second
        # comment posted; comment count stays at 1).
        before = len(gh.posted_comments)
        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        self.assertEqual(len(gh.posted_comments), before)


class HandleInReviewClosedIssueExternalMergeTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human merge with `Resolves #N` auto-closes issue N before the
    orchestrator ticks. The closed-in_review sweep yields the issue and
    `_handle_in_review` must still flip the label to `done` and stamp
    `merged_at` -- otherwise the issue stays closed-but-`in_review` forever.
    """

    def test_closed_issue_external_merge_finishes(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(MERGED_ISSUE, label="in_review")
        issue.closed = True  # Resolves #N has already auto-closed it.
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
        self.assertIn("merged_at", gh.pinned_data(MERGED_ISSUE))


class _ReviewSummaryFixtureMixin(_PatchedWorkflowMixin):
    def _setup_with_review(self, review):
        gh = FakeGitHubClient()
        issue = make_issue(REVIEW_SUMMARY_ISSUE, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=REVIEW_SUMMARY_PR,
            head_branch=_issue_branch(REVIEW_SUMMARY_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            reviews=[review],
        )
        gh.add_pr(pr)
        gh.seed_state(
            REVIEW_SUMMARY_ISSUE,
            pr_number=REVIEW_SUMMARY_PR,
            branch=_issue_branch(REVIEW_SUMMARY_ISSUE),
            dev_agent="claude",
            dev_session_id="dev-sess",
            # Watermarks below the seeded review id so the body surfaces as
            # fresh feedback. An unset summary watermark would trip the
            # legacy in_review migration and mask the review.
            pr_last_comment_id=REVIEW_SUMMARY_WATERMARK,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr


class InReviewPRReviewSummaryTest(
    unittest.TestCase,
    _ReviewSummaryFixtureMixin,
):
    """Route actionable review-summary bodies while ignoring approvals."""

    def test_change_request_body_enters_fixing(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=CHANGE_REQUEST_REVIEW_ID,
            body="please rename foo to bar in the public API",
            state="CHANGES_REQUESTED",
            user=FakeUser(HUMAN_LOGIN),
            submitted_at=long_ago,
            commit_id=REVIEWED_SHA,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # The CHANGES_REQUESTED review surfaces as fresh feedback and the
        # handler flips to `fixing` without spawning the dev or merging.
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, "validating"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        state = gh.pinned_data(REVIEW_SUMMARY_ISSUE)
        self.assertEqual(state.get("pending_fix_review_summary_max_id"), CHANGE_REQUEST_REVIEW_ID)
        # Watermark stays put so the fixing handler can read the review
        # body when it builds its dev-resume prompt.
        self.assertEqual(state.get("pr_last_review_summary_id"), 0)

    def test_comment_review_body_enters_fixing(self) -> None:
        # A "Comment" review (state=COMMENTED) needs to surface as fresh
        # feedback even though it does not block via
        # pr_has_changes_requested -- without the route to `fixing` the
        # human's note would never reach the dev session.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=COMMENT_REVIEW_ID,
            body="how about adding a smoke test for the empty-input case?",
            state="COMMENTED",
            user=FakeUser(HUMAN_LOGIN),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(REVIEW_SUMMARY_ISSUE).get("pending_fix_review_summary_max_id"),
            COMMENT_REVIEW_ID,
        )

    def test_approved_body_does_not_resume(self) -> None:
        # APPROVED reviews are excluded from the summary surface even when
        # they carry an informational body. The human approved the PR --
        # their note is not a request for changes, so the handler must not
        # route to `fixing` and must instead ping the HITL handles for
        # manual merge.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=APPROVAL_REVIEW_ID,
            body="LGTM, ship it",
            state="APPROVED",
            user=FakeUser(HUMAN_LOGIN),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.approved = True
        pr.approval_head_sha = REVIEWED_SHA

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        # No fixing route; the handler pings HITL for a manual merge.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, "done"), gh.label_history)
        ping_comments = [body for _, body in gh.posted_comments if READY_MESSAGE in body]
        self.assertEqual(len(ping_comments), 1)

    def test_empty_body_review_is_ignored(self) -> None:
        # A CHANGES_REQUESTED review with no body has nothing to forward to
        # the dev. The handler must not route to `fixing` for the empty
        # body; it falls through to the normal mergeable / ping path
        # (mergeable=True here -> the HITL ping fires).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=EMPTY_REVIEW_ID,
            body="",
            state="CHANGES_REQUESTED",
            user=FakeUser(HUMAN_LOGIN),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.changes_requested = True
        pr.changes_requested_head_sha = REVIEWED_SHA

        with patch.object(config, DEBOUNCE_SETTING, REVIEW_DEBOUNCE_SECONDS):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((REVIEW_SUMMARY_ISSUE, LABEL_FIXING), gh.label_history)
