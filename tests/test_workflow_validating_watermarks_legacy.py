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
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _agent,
)

WATERMARK_ISSUE = 300
WATERMARK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"
LEGACY_ISSUE = 500
LEGACY_PR = 1000
LEGACY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-500"
MARKER_WALK_PR = 700
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
HUMAN_FEEDBACK_ID = 950
LEGACY_ORIGINAL_COMMENT_ID = 800
LEGACY_PR_OPEN_COMMENT_ID = 960
MARKER_ONLY_COMMENT_ID = 902
APPROVAL_COMMENT_ID = 903
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
LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=1)
ORCHESTRATOR_MARKER = workflow._ORCH_COMMENT_MARKER


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
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=HUMAN_FEEDBACK_ID,
                    body="please do not merge yet",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=LEGACY_PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #1000",
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
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

        # The "do not merge yet" comment surfaces as fresh PR feedback
        # and routes the issue to `fixing` (alongside other legacy
        # comments the migration cannot reliably classify).
        self._assert_legacy_route(gh, mocks)

    def _assert_legacy_route(self, github, mocks) -> None:
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn((LEGACY_ISSUE, "done"), github.label_history)
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((LEGACY_ISSUE, LABEL_FIXING), github.label_history)
        self.assertGreaterEqual(
            github.pinned_data(LEGACY_ISSUE).get(
                "pending_fix_issue_max_id",
            ),
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
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=(
                        ":sparkles: PR opened: #700\n\n"
                        f"{ORCHESTRATOR_MARKER}"
                    ),
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
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
                    body=(
                        ":eyes: codex review requested changes\n\n"
                        f"{ORCHESTRATOR_MARKER}"
                    ),
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=APPROVAL_COMMENT_ID,
                    body=(
                        ":white_check_mark: codex review approved.\n\n"
                        f"{ORCHESTRATOR_MARKER}"
                    ),
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
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
