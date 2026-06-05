# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the core in_review routing: merged / closed-not-merged PRs, HITL ready-ping gates, PR-comment debounce, and the PR-review-summary surface."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.github import BASE_SYNC_HOLD_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
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
)


class HandleInReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Drive the in_review handler through merged / closed-not-merged /
    open-PR (HITL ready-ping gates and PR-comment debounce) branches
    against a seeded FakePR.
    """

    PR_NUMBER = 77
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-30"

    def _seed(
        self,
        *,
        issue_number: int = 30,
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
            "branch": self.BRANCH,
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
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    def test_in_review_pr_merged_externally(self) -> None:
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # Branch cleanup must fire for an external merge: the PR is gone, so
        # the per-issue worktree and the local + remote branches are dead
        # weight that should not survive past the `done` flip.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-30",
        )

    def test_in_review_pr_closed_unmerged(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # The PR is gone, so the orchestrator-owned branch and worktree
        # are dead weight regardless of whether the PR merged or was
        # declined. Cleanup must fire on the rejected terminal too.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-30",
        )

    def test_in_review_mergeable_final_docs_handoff_pings_human(self) -> None:
        # PR mergeable: post a one-shot HITL ping so the human knows the
        # PR is ready, but stay open (no merge, no label flip, no
        # awaiting_human). The orchestrator is manual-merge-only -- it
        # never calls `gh.merge_pr` from in_review. The ping must mention
        # every HITL handle so notifications fire even when the reviewer
        # agent approved via comments rather than a formal GitHub review.
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": "cafe1234",
                "docs_verdict": "no_change",
            },
        )

        with patch.object(config, "HITL_MENTIONS", "@alice @bob"):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        # Exactly one ping was posted on the issue thread.
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertIn("@alice", ping_comments[0])
        self.assertIn("@bob", ping_comments[0])
        self.assertIn(f"PR #{self.PR_NUMBER}", ping_comments[0])
        data = gh.pinned_data(30)
        # De-dup key is the head SHA we pinged for.
        self.assertEqual(data.get("ready_ping_sha"), "cafe1234")
        # Not parked: subsequent ticks must still react to comments / state.
        self.assertFalse(data.get("awaiting_human"))
        # Ping is recorded in orchestrator_comment_ids so the next tick's
        # `comments_after` filter excludes it as bot noise without needing
        # the watermark to move (which would risk swallowing a human
        # comment that landed between the earlier scan and the ping).
        ping_id = gh.latest_comment_id(issue)
        self.assertIsNotNone(ping_id)
        self.assertIn(ping_id, data.get("orchestrator_comment_ids", []))

    def test_in_review_mergeable_without_approval_signal_does_not_ping(self) -> None:
        # The ping advertises the PR as ready for review/merge; firing it
        # on a mergeable PR with neither a current final-docs handoff nor
        # a formal GitHub approval would invite a manual merge over a
        # commit no reviewer has signed off on.
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(30)
        self.assertIsNone(data.get("ready_ping_sha"))
        self.assertFalse(data.get("awaiting_human"))

    def test_in_review_mergeable_changes_requested_does_not_ping(self) -> None:
        # A standing human CHANGES_REQUESTED on the current head vetoes
        # the ping; the orchestrator must not advertise the PR as ready
        # while a human review is asking for changes, even when the
        # agent-approved final-docs handoff matches the current head.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            changes_requested=True,
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": "cafe1234",
                "docs_verdict": "updated",
            },
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(30)
        self.assertIsNone(data.get("ready_ping_sha"))

    def test_in_review_mergeable_dedups_same_head(self) -> None:
        # Second tick on the same head SHA must NOT re-ping; the ping is
        # one-shot per head so a long-lived ready-for-merge PR doesn't spam
        # the HITL handles on every poll.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        comments_after_first = list(gh.posted_comments)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.posted_comments, comments_after_first)

    def test_in_review_mergeable_repings_new_head(self) -> None:
        # A new commit on the PR branch shifts pr.head.sha; the ping is
        # keyed on the SHA we last pinged for, so the next tick must
        # re-ping on the new head.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        pings_first = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        pr.head = FakePRRef(sha="beefcafe")
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        pings_total = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(pings_first), 1)
        self.assertEqual(len(pings_total), 2)
        self.assertEqual(gh.pinned_data(30).get("ready_ping_sha"), "beefcafe")

    def test_in_review_stale_final_docs_head_does_not_ping_new_head(self) -> None:
        # The final-docs marker is a head-SHA approval signal. If another
        # commit lands after documenting, the old marker must not ping the
        # new head; the issue needs another validating/documenting pass.
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "docs_checked_sha": "cafe1234",
                "docs_verdict": "no_change",
                "ready_ping_sha": "cafe1234",
            },
        )
        pr.head = FakePRRef(sha="beefcafe")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.pinned_data(30).get("ready_ping_sha"), "cafe1234")

    def test_in_review_ready_ping_does_not_swallow_concurrent_human(self) -> None:
        # Race window: a human posts an issue comment AFTER the handler's
        # comment scan but BEFORE the ready-for-merge ping. The ping must
        # NOT bump `pr_last_comment_id` past the unseen human comment;
        # otherwise the next tick's `comments_after` would skip the human
        # feedback and the dev would never resume on it.
        from unittest.mock import patch as _patch_mock
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1500}
        )
        # Pre-seed the human comment with an id ABOVE the watermark but
        # BELOW the ping id (the fake comment-id counter starts at 1000,
        # so the next id allocated by `_post_issue_comment` will be the
        # one after this). We splice the comment in mid-handler via a
        # patch on `_post_issue_comment` so it lands AFTER the scan.
        human = FakeComment(
            id=1600, body="please hold off, doing one more pass",
            user=FakeUser("alice"), created_at=long_ago,
        )
        from orchestrator import workflow_messages
        real_post = workflow_messages._post_issue_comment

        def post_with_race(gh_arg, issue_arg, state_arg, body_arg):
            # Simulate a human comment landing right before our ping.
            if "ready for review/merge" in body_arg:
                issue_arg.comments.append(human)
            return real_post(gh_arg, issue_arg, state_arg, body_arg)

        with _patch_mock.object(workflow, "_post_issue_comment", post_with_race):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Watermark must NOT have advanced past the human comment.
        data = gh.pinned_data(30)
        self.assertLess(data.get("pr_last_comment_id"), human.id)

        # Second tick: the human comment surfaces. The fresh-feedback
        # scan now runs BEFORE the drift check, so the human comment
        # routes the issue to `fixing` (the dev is not spawned by
        # `_handle_in_review` here). The ping itself is filtered as
        # orchestrator-authored, so the route is driven by the (real,
        # human-authored) `human` comment.
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        mocks["run_agent"].assert_not_called()
        self.assertIn((30, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(30).get("pending_fix_issue_max_id"), human.id,
        )

    def test_in_review_hold_base_sync_pauses_hitl_ping(self) -> None:
        # BASE_SYNC_HOLD label suppresses the HITL ping so the operator's
        # in-progress base sync can finish without spamming the issue
        # thread.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_unmergeable_parks_for_human(self) -> None:
        # PR not mergeable: park awaiting human with
        # `park_reason="unmergeable"`. The orchestrator never routes from
        # in_review to `resolving_conflict`; the human drives the merge
        # (or relabels manually).
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        # Must NOT route to resolving_conflict.
        self.assertNotIn((30, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "unmergeable")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("not mergeable", last_comment)
        # No conflict_round seeded -- the orchestrator never enters the
        # auto-resolution route from here.
        self.assertNotIn("conflict_round", data)

    def test_in_review_hold_base_sync_skips_unmergeable_park(self) -> None:
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(30)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_in_review_mergeable_pending(self) -> None:
        # mergeable=None means GitHub is still computing. Don't ping,
        # don't park; the next tick re-checks once GitHub has decided.
        pr = self._open_pr(approved=True, mergeable=None, check_state="success")
        gh, issue = self._seed(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))

    def test_in_review_pr_comment_within_debounce_flips_to_fixing(self) -> None:
        # Fresh PR feedback inside the debounce window must NOT silently
        # wait or spawn the dev: the handler records pending-fix metadata
        # and flips the label to `fixing` immediately so the fixing handler
        # can own its own debounce / resume cycle.
        now = datetime.now(timezone.utc)
        pr = self._open_pr(
            approved=True, mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=2000, body="please tighten the docstring",
                    user=FakeUser("alice"), created_at=now,
                ),
            ],
        )
        # Watermark just below the comment so it surfaces as fresh feedback.
        # An unset watermark would trip the legacy in_review migration and
        # mask this comment as already-consumed.
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev spawn, no merge attempt (the in_review handler is not the
        # one that drives the fix any more); label flipped to `fixing`.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((30, "fixing"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertIn("pending_fix_at", data)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        # Watermarks deliberately NOT bumped: the fixing handler needs the
        # triggering comments to build its dev-resume prompt.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)

    def test_in_review_pr_comment_past_debounce_flips_to_fixing(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(
            issue_comments=[
                FakeComment(
                    id=2000, body="rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Past-debounce feedback also hands off to the fixing stage rather
        # than spawning the dev inline. The fixing handler owns the
        # resume / push / hand-back-to-`validating` cycle (a pushed fix
        # flips DIRECTLY back to `validating` for the reviewer to
        # re-evaluate; docs do not run here, the single docs pass runs
        # after reviewer approval before `in_review`).
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((30, "fixing"), gh.label_history)
        self.assertNotIn((30, "validating"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertIn("pending_fix_at", data)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)

    def test_in_review_pr_number_missing(self) -> None:
        # Manually-relabeled in_review without a pinned PR -- park once.
        gh, issue = self._seed(pr=None, with_pr_number=False)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("without a pinned `pr_number`", last_comment)

        # A second tick with awaiting_human set must NOT re-park (no second
        # comment posted; comment count stays at 1).
        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(len(gh.posted_comments), before)


class HandleInReviewClosedIssueExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merge with `Resolves #N` auto-closes issue N before the
    orchestrator ticks. The closed-in_review sweep yields the issue and
    `_handle_in_review` must still flip the label to `done` and stamp
    `merged_at` -- otherwise the issue stays closed-but-`in_review` forever.
    """

    def test_external_merge_on_closed_issue_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(40, label="in_review")
        issue.closed = True  # Resolves #N has already auto-closed it.
        gh.add_issue(issue)
        pr = FakePR(
            number=99, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-40",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(40, pr_number=99, branch="orchestrator/geserdugarov__agent-orchestrator/issue-40")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((40, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(40))


class InReviewPRReviewSummaryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human can leave PR feedback either through inline review comments
    or through the *review summary* body (the textbox above the
    Approve / Request Changes / Comment buttons). The summary lives in the
    PullRequestReview id namespace, distinct from issue comments and inline
    review comments. Without surfacing it, a "Comment" review with body or
    a CHANGES_REQUESTED summary would never be routed to `fixing` -- the
    dev would never see the feedback.
    """

    PR_NUMBER = 130
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-90"

    def _setup_with_review(self, review):
        gh = FakeGitHubClient()
        issue = make_issue(90, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            reviews=[review],
        )
        gh.add_pr(pr)
        gh.seed_state(
            90, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            # Watermarks below the seeded review id so the body surfaces as
            # fresh feedback. An unset summary watermark would trip the
            # legacy in_review migration and mask the review.
            pr_last_comment_id=999,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_changes_requested_with_body_routes_to_fixing(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242,
            body="please rename foo to bar in the public API",
            state="CHANGES_REQUESTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
            commit_id="cafe1234",
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # The CHANGES_REQUESTED review surfaces as fresh feedback and the
        # handler flips to `fixing` without spawning the dev or merging.
        mocks["run_agent"].assert_not_called()
        self.assertIn((90, "fixing"), gh.label_history)
        self.assertNotIn((90, "validating"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        data = gh.pinned_data(90)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4242)
        # Watermark stays put so the fixing handler can read the review
        # body when it builds its dev-resume prompt.
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

    def test_commented_review_with_body_routes_to_fixing(self) -> None:
        # A "Comment" review (state=COMMENTED) needs to surface as fresh
        # feedback even though it does not block via
        # pr_has_changes_requested -- without the route to `fixing` the
        # human's note would never reach the dev session.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4243,
            body="how about adding a smoke test for the empty-input case?",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((90, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(90).get("pending_fix_review_summary_max_id"),
            4243,
        )

    def test_approved_review_body_does_not_trigger_resume(self) -> None:
        # APPROVED reviews are excluded from the summary surface even when
        # they carry an informational body. The human approved the PR --
        # their note is not a request for changes, so the handler must not
        # route to `fixing` and must instead ping the HITL handles for
        # manual merge.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4244, body="LGTM, ship it", state="APPROVED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.approved = True
        pr.approval_head_sha = "cafe1234"

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # No fixing route; the handler pings HITL for a manual merge.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((90, "fixing"), gh.label_history)
        self.assertNotIn((90, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)

    def test_empty_body_review_is_ignored(self) -> None:
        # A CHANGES_REQUESTED review with no body has nothing to forward to
        # the dev. The handler must not route to `fixing` for the empty
        # body; it falls through to the normal mergeable / ping path
        # (mergeable=True here -> the HITL ping fires).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4245, body="", state="CHANGES_REQUESTED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        pr.changes_requested = True
        pr.changes_requested_head_sha = "cafe1234"

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((90, "fixing"), gh.label_history)
