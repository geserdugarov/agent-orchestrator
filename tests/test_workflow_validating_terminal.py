# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
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


class HandleValidatingExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merged the PR while the reviewer was queued. `_handle_validating`
    must short-circuit to `done` instead of running the reviewer against a
    branch that already landed.
    """

    def test_external_merge_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(120, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=12000,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-120",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            120, pr_number=12000, branch="orchestrator/geserdugarov__agent-orchestrator/issue-120",
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((120, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(120))
        self.assertTrue(issue.closed)
        # Reviewer was not spawned.
        mocks["run_agent"].assert_not_called()
        # Terminal cleanup runs for the external-merge arc.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 120,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-120",
        )


class HandleValidatingClosedIssueTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Closed `validating` issues yielded by the new closed-issue sweep
    must NOT relabel back to `in_review` via the reviewer agent. The
    handler now flips to `rejected` after the external-merge finalize
    returns False.
    """

    def test_closed_validating_with_open_pr_flips_to_rejected(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(121, label="validating")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=12100,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-121",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            121, pr_number=12100, branch="orchestrator/geserdugarov__agent-orchestrator/issue-121",
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((121, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(121))
        mocks["run_agent"].assert_not_called()
        # The PR is still open: do not clobber it via cleanup.
        mocks["_cleanup_terminal_branch"].assert_not_called()


class ValidatingApprovalRoutesThroughDocumentingTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Issue #266: after `VERDICT: APPROVED` + verify + squash/force-push,
    `_handle_validating` hands the issue off to `documenting` (not directly
    to `in_review`). `_handle_documenting`'s success exits advance to
    `in_review` unconditionally (#270 removed the `validating` fallback).
    PR watermarks, approval comment, and squash comment are preserved
    across the hop.
    """

    PR_NUMBER = 91
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-9"
    REVIEWED_SHA = "rev91"
    SQUASHED_SHA = "sq91"

    def _setup(self, **extra_state):
        gh = FakeGitHubClient()
        issue = make_issue(9, label="validating", comments=[
            FakeComment(
                id=901, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=902, body=":sparkles: PR opened: #91",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha=self.SQUASHED_SHA),
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0, pickup_comment_id=901,
            orchestrator_comment_ids=[901, 902],
        )
        state.update(extra_state)
        gh.seed_state(9, **state)
        return gh, issue, pr

    def test_approval_relabels_to_documenting(self) -> None:
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                squash_result=(True, self.SQUASHED_SHA, 2, None),
            )

        # Label hop: validating -> documenting (NOT directly in_review).
        self.assertIn((9, "documenting"), gh.label_history)
        self.assertNotIn((9, "in_review"), gh.label_history)
        data = gh.pinned_data(9)
        # Watermark, approval and squash comments all seeded before the
        # relabel and preserved across the hop.
        self.assertIsNotNone(data.get("pr_last_comment_id"))
        self.assertTrue(any(
            ":white_check_mark:" in body and "approved" in body
            for _, body in gh.posted_pr_comments
        ))
        self.assertTrue(any(
            ":package: squashed 2 commits to 1" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_verify_failure_does_not_relabel_to_documenting(self) -> None:
        # Local-verify gate fires BEFORE the approval/squash/handoff, so
        # a failed verify must leave the issue parked on `validating`
        # with no relabel to documenting or in_review.
        gh, issue, pr = self._setup()
        from orchestrator.worktrees import VerifyResult

        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                verify_result=VerifyResult(
                    status="failed", command="pytest -q",
                    exit_code=2, output="boom",
                ),
            )

        data = gh.pinned_data(9)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_failed")
        self.assertNotIn((9, "documenting"), gh.label_history)
        self.assertNotIn((9, "in_review"), gh.label_history)

    def test_squash_failure_does_not_relabel_to_documenting(self) -> None:
        # Squash failure parks awaiting human on `validating`; no
        # relabel to documenting fires, since the original commits (now
        # stale w.r.t. the operator's intended squashed head) sit on
        # the branch and the operator has to adjudicate.
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                squash_result=(False, None, 0, "force-with-lease rejected"),
            )

        data = gh.pinned_data(9)
        self.assertTrue(data.get("awaiting_human"))
        # The park comment names the failure so the operator can triage.
        self.assertTrue(any(
            "squash-on-approval failed" in body
            for _, body in gh.posted_comments
        ))
        self.assertNotIn((9, "documenting"), gh.label_history)
        self.assertNotIn((9, "in_review"), gh.label_history)
