# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _agent,
)

PR_NUMBER_OFFSET = 2_000
PUSHED_FIX_ISSUE = 301
QUESTION_ISSUE = 302
HUMAN_RESUME_ISSUE = 303
DRIFT_FIX_ISSUE = 304
DRIFT_ACK_ISSUE = 305
REVIEWER_RECOVERY_ISSUE = 306
CLEAN_DEV_RECOVERY_ISSUE = 307
PUSHED_DEV_RECOVERY_ISSUE = 308
RECOVERY_WATERMARK = 10_000
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
REVIEW_DEBOUNCE_SECONDS = 600
HANDOFF_ISSUE = 5
HANDOFF_PR = 11
HANDOFF_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-5"
SECOND_HANDOFF_ISSUE = 99
SECOND_HANDOFF_PR = 50
SECOND_HANDOFF_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-99"
CONSUMED_FEEDBACK_ID = 2000
REVIEW_FEEDBACK_WATERMARK = 4242
DEV_SESSION = "dev-sess"
BEFORE_FIX_SHA = "aaa"
REVIEW_ROUND = "review_round"
LABEL_DOCUMENTING = "documenting"
LABEL_IN_REVIEW = "in_review"
AWAITING_HUMAN = "awaiting_human"
BOT_LOGIN = "orchestrator"


class _ValidatingHandoffFixtureMixin(_PatchedWorkflowMixin):
    def _validating_issue(
        self,
        *,
        issue_number: int = 300,
        comments=(),
        body: str = "issue body",
        **state,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(
            issue_number,
            label="validating",
            body=body,
            comments=list(comments),
        )
        gh.add_issue(issue)
        defaults = dict(
            pr_number=PR_NUMBER_OFFSET + issue_number,
            branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{issue_number}",
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(issue_number, **defaults)
        return gh, issue


class ValidatingPushedFixesStayOnValidatingTest(
    unittest.TestCase,
    _ValidatingHandoffFixtureMixin,
):
    """Keep pushed fixes on validating for another review pass."""

    def test_pushed_fix_stays_validating(self) -> None:
        gh, issue = self._validating_issue(issue_number=PUSHED_FIX_ISSUE, review_round=1)
        review = _agent(
            session_id="rev-sess",
            last_message="please tighten the docstring\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id=DEV_SESSION, last_message="fixed")

        self._run_validating(
            gh,
            issue,
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha (push landed).
            head_shas=[BEFORE_FIX_SHA, "bbb"],
        )

        state = gh.pinned_data(PUSHED_FIX_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        self.assertNotIn((PUSHED_FIX_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((PUSHED_FIX_ISSUE, LABEL_IN_REVIEW), gh.label_history)

    def test_no_commit_stays_validating(self) -> None:
        # The dev asked a question instead of committing -- no push, no
        # round bump, no documenting handoff. The issue parks awaiting
        # human via `_on_question`.
        gh, issue = self._validating_issue(issue_number=QUESTION_ISSUE, review_round=1)
        review = _agent(
            session_id="rev-sess",
            last_message="why does foo do X?\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev = _agent(session_id=DEV_SESSION, last_message="not sure, ideas?")

        self._run_validating(
            gh,
            issue,
            run_agent=[review, dev],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha all equal -> no commit.
            head_shas=[BEFORE_FIX_SHA, BEFORE_FIX_SHA],
        )

        state = gh.pinned_data(QUESTION_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 1)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # Stays on validating: no documenting handoff because nothing
        # was pushed.
        self.assertNotIn((QUESTION_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((QUESTION_ISSUE, LABEL_IN_REVIEW), gh.label_history)

    def test_human_resume_push_stays_validating(self) -> None:
        gh, issue = self._validating_issue(
            issue_number=HUMAN_RESUME_ISSUE,
            awaiting_human=True,
            last_action_comment_id=PICKUP_COMMENT_ID,
            review_round=1,
            comments=[
                FakeComment(
                    id=1000,
                    body="please add a test",
                    user=FakeUser("alice"),
                ),
            ],
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="done"),
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, "bbb"],
        )

        state = gh.pinned_data(HUMAN_RESUME_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        self.assertNotIn((HUMAN_RESUME_ISSUE, LABEL_DOCUMENTING), gh.label_history)

    def test_drift_pushed_fix_stays_on_validating(self) -> None:
        gh, issue = self._validating_issue(
            issue_number=DRIFT_FIX_ISSUE,
            body="updated criteria after drift",
            user_content_hash="stale-hash",
            review_round=1,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        state = gh.pinned_data(DRIFT_FIX_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        self.assertNotIn((DRIFT_FIX_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((DRIFT_FIX_ISSUE, LABEL_IN_REVIEW), gh.label_history)

    def test_drift_ack_keeps_validating_label(self) -> None:
        # A drift ACK reply (no commit, explicit `ACK:` marker) is an
        # acknowledgement that the existing work already satisfies the
        # edit. Nothing pushed -- so we stay on `validating` to let the
        # reviewer re-run on the current head next tick.
        gh, issue = self._validating_issue(
            issue_number=DRIFT_ACK_ISSUE,
            body="updated criteria after drift",
            user_content_hash="stale-hash",
            review_round=1,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="ACK: prior commits already cover the edit.",
            ),
            dirty_files=(),
            push_branch=True,
            # No commit: before_sha == after_sha.
            head_shas=["same-sha", "same-sha"],
        )

        state = gh.pinned_data(DRIFT_ACK_ISSUE)
        # Round is NOT bumped on an ACK.
        self.assertEqual(state.get(REVIEW_ROUND), 1)
        self.assertNotIn((DRIFT_ACK_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((DRIFT_ACK_ISSUE, LABEL_IN_REVIEW), gh.label_history)
        # ACK reply was surfaced as an FYI on the issue thread.
        self.assertTrue(
            any(
                "existing work satisfies" in body
                for _, body in gh.posted_comments
            )
        )


class ValidatingRecoveryStaysOnValidatingTest(
    unittest.TestCase,
    _ValidatingHandoffFixtureMixin,
):
    """Recover transient validating parks without entering documenting."""

    def test_reviewer_recovery_keeps_label(self) -> None:
        # No commit happened during a reviewer-side park (the reviewer
        # crashed, the dev never ran). Recovery clears the flags and
        # stays on `validating` -- the PR head is unchanged.
        recovery_gh, issue = self._validating_issue(
            issue_number=REVIEWER_RECOVERY_ISSUE,
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=RECOVERY_WATERMARK,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run_validating(
                recovery_gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        recovery_state = recovery_gh.pinned_data(REVIEWER_RECOVERY_ISSUE)
        self.assertFalse(recovery_state.get(AWAITING_HUMAN))
        self.assertIsNone(recovery_state.get("park_reason"))
        # No fix landed -- stays on validating.
        self.assertEqual(recovery_state.get(REVIEW_ROUND), 1)
        self.assertNotIn((REVIEWER_RECOVERY_ISSUE, LABEL_DOCUMENTING), recovery_gh.label_history)

    def test_clean_dev_recovery_keeps_label(self) -> None:
        # The dev session timed out without producing a new commit (HEAD
        # unchanged from the pre-agent watermark). Recovery clears the
        # flags and stays on validating.
        recovery_gh, issue = self._validating_issue(
            issue_number=CLEAN_DEV_RECOVERY_ISSUE,
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
            last_action_comment_id=RECOVERY_WATERMARK,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run_validating(
                recovery_gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),  # HEAD == pre-agent SHA: no commit.
            )

        recovery_state = recovery_gh.pinned_data(CLEAN_DEV_RECOVERY_ISSUE)
        self.assertFalse(recovery_state.get(AWAITING_HUMAN))
        self.assertEqual(recovery_state.get(REVIEW_ROUND), 1)
        self.assertNotIn((CLEAN_DEV_RECOVERY_ISSUE, LABEL_DOCUMENTING), recovery_gh.label_history)

    def test_pushed_dev_recovery_stays_validating(self) -> None:
        # The dev committed before the timeout killed it; recovery
        # finishes the push. A new SHA landed on the PR but the issue
        # stays on `validating` so the reviewer re-evaluates on the
        # next tick.
        recovery_gh, issue = self._validating_issue(
            issue_number=PUSHED_DEV_RECOVERY_ISSUE,
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
            last_action_comment_id=RECOVERY_WATERMARK,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run_validating(
                recovery_gh,
                issue,
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("beef5678",),  # HEAD moved past pre-agent SHA.
            )

        recovery_state = recovery_gh.pinned_data(PUSHED_DEV_RECOVERY_ISSUE)
        self.assertEqual(recovery_state.get(REVIEW_ROUND), 2)
        self.assertNotIn((PUSHED_DEV_RECOVERY_ISSUE, LABEL_DOCUMENTING), recovery_gh.label_history)


class _ValidatingToInReviewFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(
            HANDOFF_ISSUE,
            label="validating",
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser(BOT_LOGIN),
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #11",
                    user=FakeUser(BOT_LOGIN),
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=HANDOFF_PR,
            head_branch=HANDOFF_BRANCH,
            head=FakePRRef(sha="newhead42"),
        )
        gh.add_pr(pr)
        gh.seed_state(
            HANDOFF_ISSUE,
            pr_number=HANDOFF_PR,
            branch=HANDOFF_BRANCH,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            review_round=0,
            # Pre-existing orchestrator comments are recognized by exact id,
            # not author login -- mirror what `_handle_pickup` / `_on_commits`
            # would have recorded as they posted these comments.
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr

    def _backdate_comments(self, issue, pr) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for comment in list(issue.comments) + list(pr.issue_comments):
            comment.created_at = long_ago

    def _ready_for_in_review(self, issue, pr) -> None:
        pr.approved = True
        pr.mergeable = True
        pr.check_state = "success"
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]

    def _run_debounced_in_review(self, github, issue):
        with patch.object(
            config,
            "IN_REVIEW_DEBOUNCE_SECONDS",
            REVIEW_DEBOUNCE_SECONDS,
        ):
            return self._run_in_review(
                github,
                issue,
                run_agent=_agent(),
            )

    def _assert_ready_ping(self, github) -> None:
        ping_comments = [
            body
            for _, body in github.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)


class ValidatingToInReviewHandoffTest(
    unittest.TestCase,
    _ValidatingToInReviewFixtureMixin,
):
    """Seed and ratchet feedback watermarks during approval handoff."""

    def test_approval_handoff_does_not_replay(self) -> None:
        # End-to-end: validating approves -> in_review tick pings HITL
        # without resuming the dev on the orchestrator's own automated
        # comments. This is the concrete bug guarded by the watermark
        # seeding at handoff.
        gh, issue, pr = self._setup()

        # Step 1: validating approves. This posts a PR comment, seeds the
        # watermark, and flips to `documenting` (the final-docs hop
        # before in_review).
        # Backdate every existing comment so debounce would otherwise fire.
        self._backdate_comments(issue, pr)

        mocks_v = self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=("newhead42",),
        )
        self.assertEqual(mocks_v["run_agent"].call_count, 1)
        self.assertIn((HANDOFF_ISSUE, LABEL_DOCUMENTING), gh.label_history)

        # Backdate the approval comment that pr_comment just appended too,
        # so it would falsely fire the debounce-resume path if the
        # watermark were not seeded.
        self._backdate_comments(issue, pr)

        # Step 2: pretend approved + green checks + mergeable so the
        # ready-ping gate is the thing under test.
        # Skip the documenting hop (no docs change) by relabeling to
        # in_review -- this is what `_handle_documenting`'s no-change
        # exit would do for a final-docs pass with nothing to commit.
        # Watermarks set by validating ride through untouched.
        self._ready_for_in_review(issue, pr)
        mocks_r = self._run_debounced_in_review(gh, issue)

        # Critical assertion: NO dev resume on stale orchestrator comments.
        mocks_r["run_agent"].assert_not_called()
        # The orchestrator is manual-merge-only; in_review pings HITL
        # for the manual merge instead of merging itself.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((HANDOFF_ISSUE, "done"), gh.label_history)
        self._assert_ready_ping(gh)

    def test_second_handoff_ratchets_watermark(self) -> None:
        # An earlier in_review tick consumed a human PR comment (id 2000)
        # and bounced back to validating. The dev fixed it; the reviewer
        # approves again. _seed_watermark_past_self stops at the first
        # post-pickup human comment so its recomputed seed is BELOW the
        # already-stored watermark. Without max(), pr_last_comment_id
        # would regress and the next in_review tick would replay the same
        # already-fixed feedback as "new", looping forever.
        gh = FakeGitHubClient()
        issue = make_issue(
            SECOND_HANDOFF_ISSUE,
            label="validating",
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser(BOT_LOGIN),
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #50",
                    user=FakeUser(BOT_LOGIN),
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=SECOND_HANDOFF_PR,
            head_branch=SECOND_HANDOFF_BRANCH,
            head=FakePRRef(sha="cafe9999"),
            issue_comments=[
                FakeComment(
                    id=CONSUMED_FEEDBACK_ID,
                    body="rename foo to bar",
                    user=FakeUser("alice"),
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            SECOND_HANDOFF_ISSUE,
            pr_number=SECOND_HANDOFF_PR,
            branch=SECOND_HANDOFF_BRANCH,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_last_comment_id=CONSUMED_FEEDBACK_ID,
            pr_last_review_comment_id=REVIEW_FEEDBACK_WATERMARK,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )

        # Approval relabels to `documenting` (the final-docs hop); the
        # ratcheted watermark must persist across the hop.
        self.assertIn((SECOND_HANDOFF_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        state = gh.pinned_data(SECOND_HANDOFF_ISSUE)
        watermark = state.get("pr_last_comment_id")
        self.assertGreaterEqual(
            watermark,
            CONSUMED_FEEDBACK_ID,
            f"watermark must not regress past consumed PR feedback (got {watermark})",
        )
        self.assertEqual(state.get("pr_last_review_comment_id"), REVIEW_FEEDBACK_WATERMARK)
