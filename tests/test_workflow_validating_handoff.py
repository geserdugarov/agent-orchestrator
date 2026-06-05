# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


class ValidatingPushedFixesStayOnValidatingTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Validating-side dev fixes that PUSH stay on `validating`.

    Any time the dev's fix lands on the PR branch during validating
    (CHANGES_REQUESTED, awaiting-human resume, user-content drift, or
    a transient-park recovery that finishes a push), the issue stays
    on `validating` -- the docs pass only runs as the final-docs
    handoff after a reviewer approval, not as a pre-review hop.
    """

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
            issue_number, label="validating", body=body,
            comments=list(comments),
        )
        gh.add_issue(issue)
        defaults = dict(
            pr_number=2_000 + issue_number,
            branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{issue_number}",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(issue_number, **defaults)
        return gh, issue

    def test_changes_requested_pushed_fix_stays_on_validating(self) -> None:
        gh, issue = self._validating_issue(issue_number=301, review_round=1)
        review = _agent(
            session_id="rev-sess",
            last_message="please tighten the docstring\n\n"
                         "VERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha (push landed).
            head_shas=["aaa", "bbb"],
        )

        data = gh.pinned_data(301)
        self.assertEqual(data.get("review_round"), 2)
        self.assertNotIn((301, "documenting"), gh.label_history)
        self.assertNotIn((301, "in_review"), gh.label_history)

    def test_changes_requested_no_commit_stays_on_validating(self) -> None:
        # The dev asked a question instead of committing -- no push, no
        # round bump, no documenting handoff. The issue parks awaiting
        # human via `_on_question`.
        gh, issue = self._validating_issue(issue_number=302, review_round=1)
        review = _agent(
            session_id="rev-sess",
            last_message="why does foo do X?\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev = _agent(session_id="dev-sess", last_message="not sure, ideas?")

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha all equal -> no commit.
            head_shas=["aaa", "aaa"],
        )

        data = gh.pinned_data(302)
        self.assertEqual(data.get("review_round"), 1)
        self.assertTrue(data.get("awaiting_human"))
        # Stays on validating: no documenting handoff because nothing
        # was pushed.
        self.assertNotIn((302, "documenting"), gh.label_history)
        self.assertNotIn((302, "in_review"), gh.label_history)

    def test_awaiting_human_resume_stays_on_validating_on_push(self) -> None:
        gh, issue = self._validating_issue(
            issue_number=303,
            awaiting_human=True,
            last_action_comment_id=900,
            review_round=1,
            comments=[
                FakeComment(
                    id=1000, body="please add a test",
                    user=FakeUser("alice"),
                ),
            ],
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="done"),
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        data = gh.pinned_data(303)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 2)
        self.assertNotIn((303, "documenting"), gh.label_history)

    def test_drift_pushed_fix_stays_on_validating(self) -> None:
        gh, issue = self._validating_issue(
            issue_number=304,
            body="updated criteria after drift",
            user_content_hash="stale-hash",
            review_round=1,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        data = gh.pinned_data(304)
        self.assertEqual(data.get("review_round"), 2)
        self.assertNotIn((304, "documenting"), gh.label_history)
        self.assertNotIn((304, "in_review"), gh.label_history)

    def test_drift_ack_keeps_validating_label(self) -> None:
        # A drift ACK reply (no commit, explicit `ACK:` marker) is an
        # acknowledgement that the existing work already satisfies the
        # edit. Nothing pushed -- so we stay on `validating` to let the
        # reviewer re-run on the current head next tick.
        gh, issue = self._validating_issue(
            issue_number=305,
            body="updated criteria after drift",
            user_content_hash="stale-hash",
            review_round=1,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: prior commits already cover the edit.",
            ),
            dirty_files=(),
            push_branch=True,
            # No commit: before_sha == after_sha.
            head_shas=["same-sha", "same-sha"],
        )

        data = gh.pinned_data(305)
        # Round is NOT bumped on an ACK.
        self.assertEqual(data.get("review_round"), 1)
        self.assertNotIn((305, "documenting"), gh.label_history)
        self.assertNotIn((305, "in_review"), gh.label_history)
        # ACK reply was surfaced as an FYI on the issue thread.
        self.assertTrue(any(
            "existing work satisfies" in body
            for _, body in gh.posted_comments
        ))

    def test_reviewer_timeout_recovery_keeps_validating_label(self) -> None:
        # No commit happened during a reviewer-side park (the reviewer
        # crashed, the dev never ran). Recovery clears the flags and
        # stays on `validating` -- the PR head is unchanged.
        gh, issue = self._validating_issue(
            issue_number=306,
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=10_000,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        data = gh.pinned_data(306)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # No fix landed -- stays on validating.
        self.assertEqual(data.get("review_round"), 1)
        self.assertNotIn((306, "documenting"), gh.label_history)

    def test_agent_timeout_clean_recovery_keeps_validating_label(self) -> None:
        # The dev session timed out without producing a new commit (HEAD
        # unchanged from the pre-agent watermark). Recovery clears the
        # flags and stays on validating.
        gh, issue = self._validating_issue(
            issue_number=307,
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
            last_action_comment_id=10_000,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),  # HEAD == pre-agent SHA: no commit.
            )

        data = gh.pinned_data(307)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 1)
        self.assertNotIn((307, "documenting"), gh.label_history)

    def test_agent_timeout_pushed_recovery_stays_on_validating(self) -> None:
        # The dev committed before the timeout killed it; recovery
        # finishes the push. A new SHA landed on the PR but the issue
        # stays on `validating` so the reviewer re-evaluates on the
        # next tick.
        gh, issue = self._validating_issue(
            issue_number=308,
            awaiting_human=True,
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
            last_action_comment_id=10_000,
            review_round=1,
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("beef5678",),  # HEAD moved past pre-agent SHA.
            )

        data = gh.pinned_data(308)
        self.assertEqual(data.get("review_round"), 2)
        self.assertNotIn((308, "documenting"), gh.label_history)


class ValidatingToInReviewHandoffTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The validating -> in_review handoff has to seed `pr_last_comment_id`
    as a high-watermark past every comment that already exists at handoff.
    Without this, the in_review handler sees the orchestrator's own
    ":robot: picking this up", ":sparkles: PR opened: #N", and
    ":white_check_mark: codex review approved" comments as fresh PR
    feedback once the debounce expires and resumes the dev session
    against them.
    """

    PR_NUMBER = 11
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-5"

    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #11",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="newhead42"),
        )
        gh.add_pr(pr)
        gh.seed_state(
            5,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            # Pre-existing orchestrator comments are recognized by exact id,
            # not author login -- mirror what `_handle_pickup` / `_on_commits`
            # would have recorded as they posted these comments.
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_in_review_after_approval_does_not_replay_existing_comments(self) -> None:
        # End-to-end: validating approves -> in_review tick pings HITL
        # without resuming the dev on the orchestrator's own automated
        # comments. This is the concrete bug guarded by the watermark
        # seeding at handoff.
        gh, issue, pr = self._setup()

        # Step 1: validating approves. This posts a PR comment, seeds the
        # watermark, and flips to `documenting` (the final-docs hop
        # before in_review).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Backdate every existing comment so debounce would otherwise fire.
        for c in list(issue.comments) + list(pr.issue_comments):
            c.created_at = long_ago

        mocks_v = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("newhead42",),
        )
        self.assertEqual(mocks_v["run_agent"].call_count, 1)
        self.assertIn((5, "documenting"), gh.label_history)

        # Backdate the approval comment that pr_comment just appended too,
        # so it would falsely fire the debounce-resume path if the
        # watermark were not seeded.
        for c in list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago

        # Step 2: pretend approved + green checks + mergeable so the
        # ready-ping gate is the thing under test.
        pr.approved = True
        pr.mergeable = True
        pr.check_state = "success"
        # Skip the documenting hop (no docs change) by relabeling to
        # in_review -- this is what `_handle_documenting`'s no-change
        # exit would do for a final-docs pass with nothing to commit.
        # Watermarks set by validating ride through untouched.
        from tests.fakes import FakeLabel
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks_r = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Critical assertion: NO dev resume on stale orchestrator comments.
        mocks_r["run_agent"].assert_not_called()
        # The orchestrator is manual-merge-only; in_review pings HITL
        # for the manual merge instead of merging itself.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((5, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)

    def test_second_handoff_ratchets_watermark(self) -> None:
        # An earlier in_review tick consumed a human PR comment (id 2000)
        # and bounced back to validating. The dev fixed it; the reviewer
        # approves again. _seed_watermark_past_self stops at the first
        # post-pickup human comment so its recomputed seed is BELOW the
        # already-stored watermark. Without max(), pr_last_comment_id
        # would regress and the next in_review tick would replay the same
        # already-fixed feedback as "new", looping forever.
        gh = FakeGitHubClient()
        issue = make_issue(99, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #50",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=50, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-99",
            head=FakePRRef(sha="cafe9999"),
            issue_comments=[
                FakeComment(
                    id=2000, body="rename foo to bar",
                    user=FakeUser("alice"),
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            99,
            pr_number=50,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-99",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            pr_last_comment_id=2000,
            pr_last_review_comment_id=4242,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )

        # Approval relabels to `documenting` (the final-docs hop); the
        # ratcheted watermark must persist across the hop.
        self.assertIn((99, "documenting"), gh.label_history)
        data = gh.pinned_data(99)
        wm = data.get("pr_last_comment_id")
        self.assertGreaterEqual(
            wm, 2000,
            f"watermark must not regress past consumed PR feedback (got {wm})",
        )
        self.assertEqual(data.get("pr_last_review_comment_id"), 4242)
