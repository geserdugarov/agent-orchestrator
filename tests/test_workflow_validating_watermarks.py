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
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _agent,
)

HUMAN_FEEDBACK_ISSUE = 15
HUMAN_FEEDBACK_PR = 22
HUMAN_FEEDBACK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-15"
PRE_PICKUP_ISSUE = 20
PRE_PICKUP_PR = 25
PRE_PICKUP_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-20"
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
HUMAN_FEEDBACK_ID = 950
PRE_PICKUP_COMMENT_ID = 850
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
PR_LAST_COMMENT_ID = "pr_last_comment_id"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"
RUN_AGENT = "run_agent"


class _HumanFeedbackHandoffFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(
            HUMAN_FEEDBACK_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #22",
                    user=FakeUser(BOT_LOGIN),
                ),
            ],
        )
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=HUMAN_FEEDBACK_PR,
            head_branch=HUMAN_FEEDBACK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            # Human posted a review comment during validating, BEFORE the
            # orchestrator's approval comment lands. Without the watermark
            # fix, the validating handler would seed pr_last_comment_id past
            # this comment and the next in_review tick would never see it.
            issue_comments=[
                FakeComment(
                    id=HUMAN_FEEDBACK_ID,
                    body="please add a docstring",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            HUMAN_FEEDBACK_ISSUE,
            pr_number=HUMAN_FEEDBACK_PR,
            branch=HUMAN_FEEDBACK_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr


class ValidatingHandoffPreservesHumanFeedbackTest(
    unittest.TestCase,
    _HumanFeedbackHandoffFixtureMixin,
):
    """Keep concurrent human PR feedback visible after handoff."""

    def test_human_pr_comment_survives_handoff(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. The orchestrator's approval comment
        # lands AFTER the human's. With the fix, the watermark stops at
        # the first human comment instead of swallowing it.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )
        # Validating's approval flips through `documenting` first (the
        # final-docs hop); the watermark must already be seeded past the
        # human's pre-handoff PR comment by the time the docs pass runs.
        self.assertIn((HUMAN_FEEDBACK_ISSUE, "documenting"), gh.label_history)
        watermark = gh.pinned_data(HUMAN_FEEDBACK_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertLess(
            watermark,
            HUMAN_FEEDBACK_ID,
            f"watermark must stop before human comment id=950 (got {watermark})",
        )

        # Step 2: in_review tick. The human comment is visible past the
        # watermark and the handler routes the issue to `fixing` (no dev
        # spawn here; the fixing handler drives the resume). Without the
        # surfacing, the handler would ping HITL for the manual merge
        # over the human's unaddressed feedback.
        from tests.fakes import FakeLabel

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
        # No merge happened; issue routed to `fixing` so the human's
        # feedback is owned by the fix loop.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((HUMAN_FEEDBACK_ISSUE, LABEL_FIXING), gh.label_history)
        self.assertEqual(
            gh.pinned_data(HUMAN_FEEDBACK_ISSUE).get("pending_fix_issue_max_id"),
            HUMAN_FEEDBACK_ID,
        )


class _PrePickupHandoffFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            PRE_PICKUP_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PRE_PICKUP_COMMENT_ID,
                    body="original issue clarification posted before pickup",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #25",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=PRE_PICKUP_PR,
            head_branch=PRE_PICKUP_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        gh.add_pr(pr)
        gh.seed_state(
            PRE_PICKUP_ISSUE,
            pr_number=PRE_PICKUP_PR,
            branch=PRE_PICKUP_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr

    def _run_after_handoff(self, github, issue, pr):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for comment in list(pr.issue_comments):
            if comment.created_at is None:
                comment.created_at = long_ago
        pr.approved = True
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            return self._run_in_review(
                github,
                issue,
                run_agent=_agent(),
            )

    def _assert_ready_path(self, github, mocks) -> None:
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn(
            (PRE_PICKUP_ISSUE, "done"),
            github.label_history,
        )
        self.assertNotIn(
            (PRE_PICKUP_ISSUE, LABEL_FIXING),
            github.label_history,
        )
        ping_comments = [
            body
            for _, body in github.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)


class PrePickupChatterHandoffTest(
    unittest.TestCase,
    _PrePickupHandoffFixtureMixin,
):
    """Advance the handoff watermark past pre-pickup discussion."""

    def test_pre_pickup_chatter_not_replayed(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. Watermark must include id 850 so the
        # pre-pickup human comment is treated as consumed.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEWED_SHA,),
        )
        watermark = gh.pinned_data(PRE_PICKUP_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark, "watermark must be seeded past pre-pickup")
        self.assertGreaterEqual(
            watermark,
            PR_OPEN_COMMENT_ID,
            f"watermark must advance past pre-pickup chatter and self-run; got {watermark}",
        )

        # Step 2: in_review tick. With the fix, no comment is past the
        # watermark, so the handler reaches the mergeable / HITL-ping
        # path. Without the fix, the human comment id=850 surfaces as
        # "new" and the issue routes to `fixing`.
        mocks = self._run_after_handoff(gh, issue, pr)

        # Manual-merge-only: no orchestrator merge, but the HITL ping
        # fires because the watermark fix kept the pre-pickup chatter
        # out of `new_comments`.
        self._assert_ready_path(gh, mocks)
