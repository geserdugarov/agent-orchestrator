# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config

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
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _agent,
)

ALL_WATERMARKS_ISSUE = 200
ALL_WATERMARKS_PR = 600
ALL_WATERMARKS_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-200"
WATERMARK_ISSUE = 300
INLINE_COLLISION_PR = 800
WATERMARK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
REVIEW_FEEDBACK_ID = 4242
INLINE_REVIEW_COMMENT_ID = 77
REVIEW_DEBOUNCE_SECONDS = 600
LABEL_VALIDATING = "validating"
LABEL_IN_REVIEW = "in_review"
LABEL_FIXING = "fixing"
PICKUP_MESSAGE = ":robot: orchestrator picking this up."
BOT_LOGIN = "orchestrator"
HUMAN_LOGIN = "alice"
BACKEND_CLAUDE = "claude"
DEV_SESSION = "dev-sess"
REVIEWED_SHA = "cafe1234"
CHECKS_SUCCESS = "success"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"
RUN_AGENT = "run_agent"


class _AllWatermarksHandoffFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self, *, reviews=(), review_comments=()):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            ALL_WATERMARKS_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #600",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=ALL_WATERMARKS_PR,
            head_branch=ALL_WATERMARKS_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            review_comments=list(review_comments),
            reviews=list(reviews),
        )
        gh.add_pr(pr)
        gh.seed_state(
            ALL_WATERMARKS_ISSUE,
            pr_number=ALL_WATERMARKS_PR,
            branch=ALL_WATERMARKS_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr, long_ago


class ValidatingHandoffSeedsAllWatermarksTest(
    unittest.TestCase,
    _AllWatermarksHandoffFixtureMixin,
):
    """Seed every feedback surface without consuming human reviews."""

    def test_review_summary_survives_handoff(self) -> None:
        # A "Comment" review without `CHANGES_REQUESTED` is the dangerous
        # case: it doesn't trip `pr_has_changes_requested` so the HITL
        # ping would happily advertise the PR as ready if the in_review
        # tick advanced its watermark past the body.
        gh, issue, pr, _ = self._setup(
            reviews=[
                FakePRReview(
                    id=REVIEW_FEEDBACK_ID,
                    body="please tighten the docstring",
                    state="COMMENTED",
                    user=FakeUser(HUMAN_LOGIN),
                    submitted_at=(
                        datetime.now(timezone.utc)
                        - timedelta(hours=1)
                    ),
                    commit_id=REVIEWED_SHA,
                ),
            ],
        )

        # Step 1: validating approves. Handoff must seed
        # pr_last_review_summary_id so the legacy in_review migration cannot
        # accidentally advance past the human review.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )
        state = gh.pinned_data(ALL_WATERMARKS_ISSUE)
        self.assertIn("pr_last_review_summary_id", state)
        # Seeded to 0 (or any value below the review id) -- not None and not
        # past the review.
        self.assertLess(state["pr_last_review_summary_id"], REVIEW_FEEDBACK_ID)

        # Step 2: in_review tick. The summary surfaces and the handler
        # routes the issue to `fixing` (the fixing handler owns the dev
        # resume cycle, not the in_review handler).
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((ALL_WATERMARKS_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(ALL_WATERMARKS_ISSUE).get("pending_fix_review_summary_max_id"),
            REVIEW_FEEDBACK_ID,
        )

    def test_inline_review_survives_handoff(self) -> None:
        # Same shape, inline-review surface. The orchestrator never posts
        # there either, so handoff has to seed pr_last_review_comment_id
        # explicitly.
        gh, issue, pr, _ = self._setup(
            review_comments=[
                FakeComment(
                    id=INLINE_REVIEW_COMMENT_ID,
                    body="line 4: rename foo to bar",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=(
                        datetime.now(timezone.utc)
                        - timedelta(hours=1)
                    ),
                ),
            ],
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )
        state = gh.pinned_data(ALL_WATERMARKS_ISSUE)
        self.assertIn("pr_last_review_comment_id", state)
        self.assertLess(state["pr_last_review_comment_id"], INLINE_REVIEW_COMMENT_ID)

        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((ALL_WATERMARKS_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(ALL_WATERMARKS_ISSUE).get("pending_fix_review_max_id"),
            INLINE_REVIEW_COMMENT_ID,
        )


class HandoffInlineIdCollisionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """orchestrator_comment_ids records IDs from the IssueComment namespace
    only. The validating handoff must NOT use that set to seed the inline
    review-comment watermark -- inline comments are PullRequestComment
    objects, with their own id space, where numeric collisions with bot
    issue/PR comment ids are possible. Otherwise a human inline comment
    whose id happens to match a recorded bot issue comment id would be
    treated as self-authored and consumed at handoff.
    """

    def test_bot_issue_id_collision_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            WATERMARK_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=REVIEW_FEEDBACK_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=(
                        datetime.now(timezone.utc)
                        - timedelta(hours=1)
                    ),
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=INLINE_COLLISION_PR,
            head_branch=WATERMARK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            review_comments=[
                # Same numeric id as the bot's issue comment above, but a
                # different namespace (PullRequestComment). The handoff must
                # not treat this as self-authored.
                FakeComment(
                    id=REVIEW_FEEDBACK_ID,
                    body="please rename foo to bar",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=(
                        datetime.now(timezone.utc)
                        - timedelta(hours=1)
                    ),
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            WATERMARK_ISSUE,
            pr_number=INLINE_COLLISION_PR,
            branch=WATERMARK_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[REVIEW_FEEDBACK_ID],
            pickup_comment_id=REVIEW_FEEDBACK_ID,
        )

        # Step 1: validating handoff. The inline comment must NOT bump
        # pr_last_review_comment_id past 4242.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )
        state = gh.pinned_data(WATERMARK_ISSUE)
        self.assertLess(
            state.get("pr_last_review_comment_id"),
            REVIEW_FEEDBACK_ID,
            "id collision must not advance the inline-review watermark",
        )

        # Step 2: in_review tick. The human's inline comment surfaces and
        # routes the issue to `fixing` -- no ready-for-merge ping. The
        # fixing handler owns the dev resume on the next tick.
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((WATERMARK_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(WATERMARK_ISSUE).get("pending_fix_review_max_id"),
            REVIEW_FEEDBACK_ID,
        )
