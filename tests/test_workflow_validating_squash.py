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

APPROVAL_ISSUE = 5
APPROVAL_PR = 31
APPROVAL_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-5"
REVIEWED_SHA = "reviewedAA"
SQUASHED_SHA = "squashedBB"
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
REVIEW_DEBOUNCE_SECONDS = 600
SQUASH_ON_APPROVAL = "SQUASH_ON_APPROVAL"
LABEL_DOCUMENTING = "documenting"


class _SquashApprovalFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            APPROVAL_ISSUE,
            label="validating",
            title="add a feature",
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #31",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        # PR head SHA mirrors the post-squash remote head -- the force-push
        # inside the squash helper updates the remote, so by the time the
        # next gh.get_pr() is taken (inside _handle_validating's seeding
        # block, AND on the next in_review tick) the remote head matches
        # the new local SHA.
        pr = FakePR(
            number=APPROVAL_PR,
            head_branch=APPROVAL_BRANCH,
            head=FakePRRef(sha=SQUASHED_SHA),
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            APPROVAL_ISSUE,
            pr_number=APPROVAL_PR,
            branch=APPROVAL_BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr

    def _run_squash_approval(
        self,
        github,
        issue,
        squash_result,
    ):
        with patch.object(config, SQUASH_ON_APPROVAL, True):
            return self._run_validating(
                github,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
                squash_result=squash_result,
            )

    def _assert_squash_handoff(self, github, pr, mocks) -> None:
        self.assertEqual(
            mocks["_squash_and_force_push"].call_count,
            1,
        )
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            (APPROVAL_ISSUE, LABEL_DOCUMENTING),
            github.label_history,
        )
        state = github.pinned_data(APPROVAL_ISSUE)
        squash_notice_posted = any(
            ":package: squashed 3 commits to 1" in body
            for _, body in github.posted_pr_comments
        )
        self.assertTrue(
            squash_notice_posted,
            f"squash notice not posted; got: {github.posted_pr_comments}",
        )
        approval_and_squash_ids = [
            comment.id
            for comment in pr.issue_comments
        ]
        self.assertTrue(approval_and_squash_ids)
        self.assertGreaterEqual(
            state.get("pr_last_comment_id"),
            max(approval_and_squash_ids),
            "watermark must include approval and squash comments",
        )

    def _run_review_after_squash(self, github, issue, pr):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for comment in list(issue.comments) + list(pr.issue_comments):
            if comment.created_at is None:
                comment.created_at = long_ago
        pr.approved = True
        if not any(label.name == "in_review" for label in issue.labels):
            issue.labels = [FakeLabel("in_review")]
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

    def _assert_ready_ping(self, github, mocks) -> None:
        mocks["run_agent"].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn(
            (APPROVAL_ISSUE, "done"),
            github.label_history,
        )
        ping_comments = [
            body
            for _, body in github.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            github.pinned_data(APPROVAL_ISSUE).get("ready_ping_sha"),
            SQUASHED_SHA,
        )

    def _assert_squash_parked(self, github, mocks) -> None:
        self.assertEqual(
            mocks["_squash_and_force_push"].call_count,
            1,
        )
        state = github.pinned_data(APPROVAL_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        park_posted = any(
            "squash-on-approval failed" in body
            for _, body in github.posted_comments
        )
        self.assertTrue(
            park_posted,
            f"HITL park message not posted; got: {github.posted_comments}",
        )
        self.assertNotIn(
            (APPROVAL_ISSUE, "in_review"),
            github.label_history,
            "park must not relabel to in_review",
        )
        self.assertNotIn(
            (APPROVAL_ISSUE, LABEL_DOCUMENTING),
            github.label_history,
            "park must not start the final-docs hop",
        )


class SquashOnApprovalTest(
    unittest.TestCase,
    _SquashApprovalFixtureMixin,
):
    """Squash approved branches and preserve the approval handoff."""

    def test_lands_in_review_without_re_review(
        self,
    ) -> None:
        # End-to-end: validating approves, squash + force-push runs (mocked
        # to succeed), the squash PR comment is posted, the issue lands in
        # in_review, and the next in_review tick pings HITL WITHOUT
        # spawning the reviewer on the rewritten head.
        gh, issue, pr = self._setup()

        mocks_v = self._run_squash_approval(
            gh,
            issue,
            (True, SQUASHED_SHA, 3, None),
        )

        # Squash helper was called exactly once on the approval path.
        self._assert_squash_handoff(gh, pr, mocks_v)

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # Approved + mergeable; the ping MUST fire and must NOT re-run
        # the reviewer agent (its run_agent call would otherwise be
        # visible in mocks_r below).
        mocks_r = self._run_review_after_squash(gh, issue, pr)
        # The orchestrator is manual-merge-only: the post-squash head
        # earns a HITL ping for the human to merge by hand. No
        # orchestrator-initiated merge call fires.
        self._assert_ready_ping(gh, mocks_r)

    def test_failure_parks_without_relabel(self) -> None:
        # Push rejected / lease violation / dirty tree all surface as
        # `success=False`. The orchestrator parks awaiting_human, leaves
        # the issue in `validating`, and does NOT seed watermarks (the
        # original commits remain on the branch and a human can decide
        # what to do).
        gh, issue, pr = self._setup()

        mocks = self._run_squash_approval(
            gh,
            issue,
            (
                False,
                None,
                0,
                "force-push with lease rejected (concurrent update)",
            ),
        )

        # Park happened: awaiting_human flag set, HITL message posted to
        # the issue thread.
        self._assert_squash_parked(gh, mocks)

    def test_squash_off_preserves_legacy_behavior(self) -> None:
        # Kill switch: with SQUASH_ON_APPROVAL=off the squash helper must
        # NOT be called and no squash notice is posted.
        gh, issue, pr = self._setup()
        # Make pr.head.sha match REVIEWED_SHA -- legacy path: the local
        # HEAD the reviewer saw is what the remote PR points at, since no
        # force-push happened.
        pr.head = FakePRRef(sha=REVIEWED_SHA)

        with patch.object(config, SQUASH_ON_APPROVAL, False):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
            )

        # Helper not called at all.
        mocks["_squash_and_force_push"].assert_not_called()
        # No squash notice posted.
        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # And the legacy approval flow flips to `documenting` (the
        # final-docs hop) regardless of SQUASH_ON_APPROVAL.
        self.assertIn((APPROVAL_ISSUE, LABEL_DOCUMENTING), gh.label_history)

    def test_single_commit_posts_no_notice(self) -> None:
        # The helper returns `squashed_count=0` when there's only one
        # commit on top of base -- nothing to squash. The orchestrator
        # must skip the squash PR comment (the helper returns the same
        # SHA back).
        gh, issue, pr = self._setup()
        pr.head = FakePRRef(sha=REVIEWED_SHA)

        with patch.object(config, SQUASH_ON_APPROVAL, True):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
                # Helper success no-op: nothing to squash.
                squash_result=(True, REVIEWED_SHA, 0, None),
            )

        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # Approval still flips to `documenting` (the final-docs hop)
        # even when there's only one commit (so no squash notice).
        self.assertIn((APPROVAL_ISSUE, LABEL_DOCUMENTING), gh.label_history)
