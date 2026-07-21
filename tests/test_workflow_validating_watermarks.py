# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config, workflow

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
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

HUMAN_FEEDBACK_ISSUE = 15
HUMAN_FEEDBACK_PR = 22
HUMAN_FEEDBACK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-15"
PRE_PICKUP_ISSUE = 20
PRE_PICKUP_PR = 25
PRE_PICKUP_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-20"
ALL_WATERMARKS_ISSUE = 200
ALL_WATERMARKS_PR = 600
ALL_WATERMARKS_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-200"
WATERMARK_ISSUE = 300
INLINE_COLLISION_PR = 800
WATERMARK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"
LEGACY_ISSUE = 500
LEGACY_PR = 1000
LEGACY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-500"
MARKER_WALK_PR = 700
CONSUMED_REPLY_ISSUE = 900
CONSUMED_REPLY_PR = 1500
CONSUMED_REPLY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-900"
RESUME_WATERMARK_ISSUE = 901
ISSUE_THREAD_ISSUE = 800
ISSUE_THREAD_PR = 1600
ISSUE_THREAD_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-800"
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
HUMAN_FEEDBACK_ID = 950
PRE_PICKUP_COMMENT_ID = 850
LEGACY_ORIGINAL_COMMENT_ID = 800
LEGACY_PR_OPEN_COMMENT_ID = 960
REVIEW_FEEDBACK_ID = 4242
INLINE_REVIEW_COMMENT_ID = 77
MARKER_ONLY_COMMENT_ID = 902
APPROVAL_COMMENT_ID = 903
PARK_COMMENT_ID = 910
CONSUMED_REPLY_ID = 920
PR_OPEN_AFTER_RESUME_ID = 930
LATEST_REPLY_ID = 921
UNREAD_PR_COMMENT_ID = 915
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
        return gh, issue, pr, long_ago


class PrePickupChatterHandoffTest(
    unittest.TestCase,
    _PrePickupHandoffFixtureMixin,
):
    """Advance the handoff watermark past pre-pickup discussion."""

    def test_pre_pickup_chatter_not_replayed(self) -> None:
        gh, issue, pr, long_ago = self._setup()

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

        # Backdate the approval comment too so debounce wouldn't filter it
        # out as a confound (it shouldn't matter because the watermark
        # already covers it, but be explicit).
        for comment in list(pr.issue_comments):
            if comment.created_at is None:
                comment.created_at = long_ago

        # Step 2: in_review tick. With the fix, no comment is past the
        # watermark, so the handler reaches the mergeable / HITL-ping
        # path. Without the fix, the human comment id=850 surfaces as
        # "new" and the issue routes to `fixing`.
        pr.approved = True
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
        # Manual-merge-only: no orchestrator merge, but the HITL ping
        # fires because the watermark fix kept the pre-pickup chatter
        # out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((PRE_PICKUP_ISSUE, "done"), gh.label_history)
        self.assertNotIn((PRE_PICKUP_ISSUE, LABEL_FIXING), gh.label_history)
        ping_comments = [body for _, body in gh.posted_comments if "ready for review/merge" in body]
        self.assertEqual(len(ping_comments), 1)


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
        long_ago_review = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=REVIEW_FEEDBACK_ID,
            body="please tighten the docstring",
            state="COMMENTED",
            user=FakeUser(HUMAN_LOGIN),
            submitted_at=long_ago_review,
            commit_id=REVIEWED_SHA,
        )
        gh, issue, pr, _ = self._setup(reviews=[review])

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
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr, _ = self._setup(
            review_comments=[
                FakeComment(
                    id=INLINE_REVIEW_COMMENT_ID,
                    body="line 4: rename foo to bar",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
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
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            WATERMARK_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=REVIEW_FEEDBACK_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
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
                    created_at=long_ago,
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


class HandoffWithoutPickupIdLegacyStateTest(unittest.TestCase, _PatchedWorkflowMixin):
    """For an issue picked up under an older orchestrator version that did
    not record `pickup_comment_id`, the validating handoff cannot tell
    pre-pickup chatter (safe to skip) from human feedback posted during
    implementing/validating (must preserve). The seed-watermark function
    must refuse to advance past anything in that legacy state, defaulting
    pr_last_comment_id to 0; the orchestrator_comment_ids id-set filter in
    `_handle_in_review` then drops the recorded bot comments at scan time
    while leaving every human comment visible.
    """

    def test_legacy_human_comment_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Comment id ordering models a real legacy lifecycle: pre-pickup
        # chatter, then a pickup posted by the OLD orchestrator (id 900,
        # NOT recorded in orchestrator_comment_ids), then a human "do not
        # merge yet" posted while the dev was implementing, then a
        # PR-opened comment posted by the NEW orchestrator (id 960,
        # recorded). The human comment between the two bot posts is the
        # signal that must NOT be lost.
        issue = make_issue(
            LEGACY_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=LEGACY_ORIGINAL_COMMENT_ID,
                    body="original issue clarification",
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
                    id=HUMAN_FEEDBACK_ID,
                    body="please do not merge yet",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=LEGACY_PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #1000",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=LEGACY_PR,
            head_branch=LEGACY_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        gh.add_pr(pr)
        # Legacy state: PR-opened (960) is the FIRST recorded bot id;
        # pickup_comment_id is missing because pickup happened under the
        # old code. Validating handoff will then see only {960} as
        # orchestrator content; the seed-watermark function must NOT
        # falsely treat ids 800/900/950 as pre-pickup chatter.
        gh.seed_state(
            LEGACY_ISSUE,
            pr_number=LEGACY_PR,
            branch=LEGACY_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[LEGACY_PR_OPEN_COMMENT_ID],
        )

        # Step 1: validating approves. Handoff must NOT advance the
        # watermark past 950.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )
        watermark = gh.pinned_data(LEGACY_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertLess(
            watermark,
            HUMAN_FEEDBACK_ID,
            f"watermark must not consume legacy human feedback at id 950 (got {watermark})",
        )

        # Step 2: in_review tick. Every gate passes -- the only thing
        # standing between the PR and a ready-ping is the human's "do
        # not merge yet" comment. The handler must surface it as fresh
        # feedback and route to `fixing`.
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

        # No merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((LEGACY_ISSUE, "done"), gh.label_history)
        # The "do not merge yet" comment surfaces as fresh PR feedback
        # and routes the issue to `fixing` (alongside other legacy
        # comments the migration cannot reliably classify).
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((LEGACY_ISSUE, LABEL_FIXING), gh.label_history)
        # The legacy default falls through to scan from the beginning,
        # so the route bookmarks the latest visible human/issue-side id.
        self.assertGreaterEqual(
            gh.pinned_data(LEGACY_ISSUE).get("pending_fix_issue_max_id"),
            HUMAN_FEEDBACK_ID,
        )


class HandoffWalkerHonorsOrchestratorMarkerTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_seed_watermark_past_self` must recognise orchestrator-authored
    content by hidden body marker AS WELL AS by recorded id. Without the
    marker check, a bot comment whose id was evicted from the bounded
    `orchestrator_comment_ids` cap (or never persisted due to a state-write
    race) stops the walker early and leaves the in_review watermark
    stranded at the previous orchestrator id. The in_review filter still
    drops the marker-bearing bot content at scan time, so no human comment
    is mis-routed, but the validating->documenting->in_review->fixing
    cycle described in #437 made the cost of that stranded watermark
    visible: every in_review tick re-scanned the same orchestrator backlog,
    and the fixing-bounce path silently relabelled to `validating`,
    looping the issue indefinitely.
    """

    def test_marker_only_comment_does_not_stop_walk(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        marker = workflow._ORCH_COMMENT_MARKER
        # Lifecycle: pickup (900) -> PR-opened (901) -> orchestrator
        # reviewer-requested-changes comment whose id was never tracked
        # (902, marker present, MISSING from orchestrator_comment_ids) ->
        # orchestrator approval (903, tracked). The id=902 case models the
        # PR #433 incident where 3 review-request comments were posted
        # but their ids never made it into `orchestrator_comment_ids` -- a
        # state-write race window.
        issue = make_issue(
            WATERMARK_ISSUE,
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
                    body=f":sparkles: PR opened: #700\n\n{marker}",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=MARKER_WALK_PR,
            head_branch=WATERMARK_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=MARKER_ONLY_COMMENT_ID,
                    body=(f":eyes: codex review requested changes\n\n{marker}"),
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=APPROVAL_COMMENT_ID,
                    body=f":white_check_mark: codex review approved.\n\n{marker}",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        # Crucially: id 902 is NOT in orchestrator_comment_ids. Without
        # the marker check the walker would stop at 902 and leave the
        # watermark at 901.
        gh.seed_state(
            WATERMARK_ISSUE,
            pr_number=MARKER_WALK_PR,
            branch=WATERMARK_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[
                PICKUP_COMMENT_ID,
                PR_OPEN_COMMENT_ID,
                APPROVAL_COMMENT_ID,
            ],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )

        # Watermark must advance past the marker-only id 902 -- ideally
        # to id 903 (the latest tracked orchestrator comment on either
        # surface). The exact value is not part of the contract, but it
        # must NOT be stuck at <=901.
        watermark = gh.pinned_data(WATERMARK_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertGreaterEqual(
            watermark,
            MARKER_ONLY_COMMENT_ID,
            f"walker must advance past marker-only bot comment id=902; got {watermark}",
        )


class HandoffSkipsConsumedRepliesTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human reply consumed by `_resume_developer_on_human_reply` during
    implementing or validating must not re-surface as fresh PR feedback in
    in_review. The validating handoff watermark seed has to walk past such
    already-consumed comments; otherwise the next in_review tick re-routes
    the issue to `fixing` on the same human input the dev has already
    addressed.
    """

    def test_consumed_reply_not_replayed(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> implementing dev asks question, parks
        # at 910 -> human replies "use sqlite" at 920 -> next tick resumes
        # the dev with that comment -> dev commits, _on_commits posts
        # PR-opened at 930 -> validating reviewer approves and posts
        # approval comment at 940. The reply at 920 was already fed to
        # the dev; in_review must NOT replay it.
        issue = make_issue(
            CONSUMED_REPLY_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PARK_COMMENT_ID,
                    body="@hitl agent needs your input to proceed",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=CONSUMED_REPLY_ID,
                    body="use sqlite please",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_AFTER_RESUME_ID,
                    body=":sparkles: PR opened: #1500",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=CONSUMED_REPLY_PR,
            head_branch=CONSUMED_REPLY_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        gh.add_pr(pr)
        # `last_action_comment_id=920` reflects the post-resume bump --
        # the resume ate comments after the park (910) up through 920.
        gh.seed_state(
            CONSUMED_REPLY_ISSUE,
            pr_number=CONSUMED_REPLY_PR,
            branch=CONSUMED_REPLY_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[
                PICKUP_COMMENT_ID,
                PARK_COMMENT_ID,
                PR_OPEN_AFTER_RESUME_ID,
            ],
            pickup_comment_id=PICKUP_COMMENT_ID,
            last_action_comment_id=CONSUMED_REPLY_ID,
        )

        # Step 1: validating approves. The handoff seed must walk PAST
        # comment 920 (already consumed) instead of stopping at it.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEWED_SHA,),
        )
        watermark = gh.pinned_data(CONSUMED_REPLY_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertGreaterEqual(
            watermark,
            PR_OPEN_AFTER_RESUME_ID,
            f"watermark must advance past consumed reply (id 920); got {watermark}",
        )

        # Step 2: in_review tick. Comment 920 must NOT surface and the
        # handler reaches the manual-merge HITL ping path.
        pr.approved = True
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
        # Manual-merge-only: no merge call. The HITL ping fires because
        # the seed kept the consumed reply out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((CONSUMED_REPLY_ISSUE, "done"), gh.label_history)
        self.assertNotIn(
            (CONSUMED_REPLY_ISSUE, LABEL_FIXING),
            gh.label_history,
        )
        ping_comments = [body for _, body in gh.posted_comments if "ready for review/merge" in body]
        self.assertEqual(len(ping_comments), 1)

    def test_resume_bumps_last_action_to_consumed_max(self) -> None:
        # Direct unit-level check on `_resume_developer_on_human_reply`:
        # after the resume runs, `last_action_comment_id` must reflect
        # the highest consumed id, not the prior park id.

        gh = FakeGitHubClient()
        issue = make_issue(
            RESUME_WATERMARK_ISSUE,
            label="implementing",
            comments=[
                FakeComment(id=PARK_COMMENT_ID, body="park", user=FakeUser(BOT_LOGIN)),
                FakeComment(id=CONSUMED_REPLY_ID, body="use sqlite", user=FakeUser(HUMAN_LOGIN)),
                FakeComment(id=LATEST_REPLY_ID, body="and add a test", user=FakeUser(HUMAN_LOGIN)),
            ],
        )
        gh.add_issue(issue)
        gh.seed_state(
            RESUME_WATERMARK_ISSUE,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            last_action_comment_id=PARK_COMMENT_ID,
        )
        state = gh.read_pinned_state(issue)

        with (
            patch.object(
                workflow,
                "_ensure_worktree",
                lambda spec, issue_number, **_: _FAKE_WT,
            ),
            patch.object(workflow, RUN_AGENT, lambda *args, **kwargs: _agent()),
        ):
            resume_result = workflow._resume_developer_on_human_reply(gh, _TEST_SPEC, issue, state)

        self.assertIsNotNone(resume_result)
        self.assertEqual(
            state.get("last_action_comment_id"),
            LATEST_REPLY_ID,
            "resume must bump last_action_comment_id to max(consumed)",
        )


class HandoffConsumedThroughIssueThreadOnlyTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`last_action_comment_id` only records issue-thread comments fed via
    `_resume_developer_on_human_reply`; PR-conversation comments are never
    consumed via that path. The validating handoff seed must NOT apply
    `consumed_through` to the PR-conversation surface, or a human PR comment
    whose id sits below a later-consumed issue-thread reply gets silently
    advanced past and the HITL ping fires over unread feedback.
    """

    def test_pr_comment_below_consumed_max_is_kept(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> park asking question (910) -> human
        # leaves a PR-conv comment at 915 (the one that MUST surface) ->
        # human also replies on the issue thread at 920 -> resume consumes
        # the issue reply and bumps `last_action_comment_id` to 920 ->
        # PR-opened comment at 930 -> validating reviewer approves and
        # posts approval at 940. The PR-conv comment at 915 was never fed
        # to the dev (validating only watches the issue thread); without
        # the fix the seed walks past it because 915 <= consumed_through
        # (920) and the next tick pings HITL over it.
        issue = make_issue(
            ISSUE_THREAD_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PARK_COMMENT_ID,
                    body="@hitl agent needs your input to proceed",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=CONSUMED_REPLY_ID,
                    body="use sqlite please",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_AFTER_RESUME_ID,
                    body=":sparkles: PR opened: #1600",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=ISSUE_THREAD_PR,
            head_branch=ISSUE_THREAD_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=UNREAD_PR_COMMENT_ID,
                    body="please add a docstring to the public class",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            ISSUE_THREAD_ISSUE,
            pr_number=ISSUE_THREAD_PR,
            branch=ISSUE_THREAD_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[
                PICKUP_COMMENT_ID,
                PARK_COMMENT_ID,
                PR_OPEN_AFTER_RESUME_ID,
            ],
            pickup_comment_id=PICKUP_COMMENT_ID,
            last_action_comment_id=CONSUMED_REPLY_ID,
        )

        # Step 1: validating approves and seeds in_review watermarks. The
        # seed must stop before 915 so the next in_review tick scans the
        # PR-conv surface and finds the human comment. Approval routes
        # through `documenting` first (the final-docs hop).
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEWED_SHA,),
        )
        self.assertIn((ISSUE_THREAD_ISSUE, "documenting"), gh.label_history)
        watermark = gh.pinned_data(ISSUE_THREAD_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertLess(
            watermark,
            UNREAD_PR_COMMENT_ID,
            "watermark must stop before unread PR-conv comment id=915 "
            f"(consumed_through=920 must NOT apply across surfaces); got {watermark}",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # The PR-conv comment surfaces and the handler routes the issue
        # to `fixing` (the fixing handler owns the dev resume on the
        # next tick) instead of pinging HITL.
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

        # Routed to fixing -- the unread PR-conv text is bookmarked for
        # the fixing handler. No HITL ping fires over unread feedback.
        # `pending_fix_issue_max_id` covers BOTH the issue-thread and
        # PR-conversation surfaces (they share the IssueComment id space);
        # 915 was the unread PR-conv comment, 920 was the issue-thread
        # human reply that consumed_through skipped at handoff but
        # in_review re-scans regardless, so the max across the bucket is
        # 920. The point of the test is that 915 has to be visible to
        # the fixing handler -- it must sit at or below the bookmark and
        # past the watermark.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((ISSUE_THREAD_ISSUE, LABEL_FIXING), gh.label_history)
        state = gh.pinned_data(ISSUE_THREAD_ISSUE)
        self.assertGreaterEqual(state.get("pending_fix_issue_max_id"), UNREAD_PR_COMMENT_ID)
        # The watermark stays put so the fixing handler can re-scan and
        # see id 915.
        self.assertLess(state.get(PR_LAST_COMMENT_ID), UNREAD_PR_COMMENT_ID)
