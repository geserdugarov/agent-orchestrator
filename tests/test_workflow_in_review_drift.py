# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the in_review drift / fresh-feedback routes: pushed and ACK exits to validating, park-on-failure, and the fresh-feedback scan that covers both the issue thread and the PR-conversation surface."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleInReviewResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_pushed_bounces_directly_to_validating(
        self,
    ) -> None:
        # The in_review handler must mirror the comment-driven dev resume:
        # post a notice on the PR (not just the issue), resume the locked
        # dev session with the new body, push the fix, and bounce
        # DIRECTLY back to `validating` so the reviewer re-evaluates the
        # updated body / new head. Docs do not run on the drift exit --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff, so running the docs
        # stage against an unapproved diff here would just push a no-op
        # and waste a tick.
        gh = FakeGitHubClient()
        issue = make_issue(80, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=800, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-80")
        gh.add_pr(pr)
        gh.seed_state(
            80,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-80",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="addressed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        # Bounced directly to validating after the pushed drift resume.
        self.assertIn((80, "validating"), gh.label_history)
        # And NOT through documenting -- docs run after reviewer
        # approval before `in_review`, not on the drift exit.
        self.assertNotIn((80, "documenting"), gh.label_history)
        # Notice posted on the PR conversation surface.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_pr_comments
        ))
        data = gh.pinned_data(80)
        # New hash persisted.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # review_round reset because this is a new diff.
        self.assertEqual(data.get("review_round"), 0)

    def test_body_drift_ack_bounces_directly_to_validating(self) -> None:
        # A drift ACK reply (no commit, explicit `ACK:` marker) is an
        # acknowledgement that the existing work already satisfies the
        # edit. The issue bounces DIRECTLY back to `validating` (same
        # destination as the pushed-fix exit; docs do not run on the
        # drift exit, the single docs pass runs after reviewer approval
        # before `in_review` via the final-docs handoff). `review_round`
        # is reset so the reviewer round cap counts fresh rounds.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=801, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-81")
        gh.add_pr(pr)
        gh.seed_state(
            81,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-81",
            review_round=2,
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: prior commits already satisfy the edit.",
            ),
            dirty_files=(),
            push_branch=True,
            # No commit landed -- before/after SHA match.
            head_shas=["same-sha", "same-sha"],
        )

        # Bounced directly to validating (same destination as the
        # pushed-fix exit; docs do not run on the drift exit, the
        # single docs pass runs after reviewer approval before
        # `in_review`).
        self.assertIn((81, "validating"), gh.label_history)
        self.assertNotIn((81, "documenting"), gh.label_history)
        data = gh.pinned_data(81)
        # `review_round` reset so the reviewer round cap counts fresh.
        self.assertEqual(data.get("review_round"), 0)
        # ACK was surfaced as an FYI on the issue thread (matches the
        # `_post_user_content_change_result` ack branch).
        self.assertTrue(any(
            "existing work satisfies" in body
            for _, body in gh.posted_comments
        ))

    def test_body_drift_park_does_not_relabel(self) -> None:
        # On a parked outcome (timeout / dirty / push fail / no-commit
        # without ACK) the handler must NOT flip to validating OR
        # documenting -- the dev fix didn't land and the issue stays
        # in `in_review` awaiting human. Preserves the failure-path
        # contract while the success / ACK paths both bounce directly
        # back to `validating`.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=802, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-82")
        gh.add_pr(pr)
        gh.seed_state(
            82,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-82",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
            head_shas=["before"],
        )

        # Did NOT advance into documenting / validating; awaiting human
        # in `in_review`.
        self.assertNotIn((82, "documenting"), gh.label_history)
        self.assertNotIn((82, "validating"), gh.label_history)
        data = gh.pinned_data(82)
        self.assertTrue(data.get("awaiting_human"))

    def test_body_drift_interrupted_resume_is_ignored(self) -> None:
        # A shutdown-killed (interrupted) drift resume must be ignored
        # entirely: the handler bails WITHOUT bumping the in_review
        # watermarks or writing, so the pre-staged `user_content_hash`
        # refresh, consumed drift comments, `last_agent_action_at`, and the
        # `awaiting_human` clear from `_resume_dev_with_text` never reach
        # GitHub. The next tick re-detects the body change and retries.
        gh = FakeGitHubClient()
        issue = make_issue(83, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=803, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-83")
        gh.add_pr(pr)
        gh.seed_state(
            83,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-83",
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                interrupted=True,
                last_message="partial drift fix before the shutdown SIGTERM",
            ),
            head_shas=["before"],
        )

        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        # Nothing persisted: the interrupted resume is ignored.
        self.assertEqual(gh.write_state_calls, 0)
        self.assertNotIn((83, "validating"), gh.label_history)
        self.assertNotIn((83, "documenting"), gh.label_history)
        data = gh.pinned_data(83)
        # Drift NOT consumed: the stale hash stands so the next tick fires.
        self.assertEqual(data.get("user_content_hash"), "stale-hash")
        self.assertFalse(data.get("awaiting_human"))

    def test_body_drift_no_commit_publishes_stranded_fix(self) -> None:
        # A no-commit drift resume that finds a committed-but-unpublished
        # fix stranded on the branch (e.g. left by a PRIOR interrupted drift
        # resume that committed before being killed) must PUBLISH it through
        # the push tail and report "pushed" -- even when the reply carries an
        # `ACK:` marker. Without the stranded-fix gate the ACK would return
        # "ack" and the caller would consume/advance the drift while the PR
        # branch never received the commit. Mirrors `_handle_dev_fix_result`.
        gh = FakeGitHubClient()
        issue = make_issue(84, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        pr = FakePR(number=804, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-84")
        gh.add_pr(pr)
        gh.seed_state(
            84,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-84",
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: existing work already satisfies the edit",
            ),
            head_shas=["same-sha", "same-sha"],  # no NEW commit this run
            push_branch=True,
            # HEAD is strictly ahead of the remote branch -> a stranded,
            # committed-but-unpushed fix exists.
            branch_ahead_behind=(1, 0),
        )

        # The stranded fix is published instead of acked.
        mocks["_push_branch"].assert_called_once()
        # "pushed" outcome bounces directly to validating with a fresh round.
        self.assertIn((84, "validating"), gh.label_history)
        self.assertEqual(gh.pinned_data(84).get("review_round"), 0)
        # The misleading "satisfies the edit" FYI is NOT posted (we published
        # a real commit, not an acknowledgement).
        self.assertFalse(any(
            "satisfies the edit" in body for _, body in gh.posted_comments
        ))


class InReviewFreshFeedbackRouteCoversBothSurfacesTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Issue-thread and PR-conversation comments share the IssueComment id
    space. The fresh-feedback scan must surface both before the drift
    check runs, otherwise the drift path's `user_content_hash` (which
    only sees the issue thread) would catch the issue-thread comment and
    forward it through the dev-resume path, leaving the PR-conversation
    comment for a later bump to silently consume. By scanning both
    surfaces together and bookmarking the max id across them, the
    fixing route preserves both comments for the (future real) fix
    handler."""

    def test_concurrent_issue_thread_and_pr_conv_both_bookmarked(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1300, label="in_review", body="updated body",
        )
        # Issue-thread comment with id 200.
        issue.comments.append(FakeComment(
            id=200, body="adds an acceptance criterion",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        pr = FakePR(number=13000, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-1300")
        # Concurrent PR-conversation comment at id 150 (between the
        # prior watermark and the issue-thread max).
        pr.issue_comments.append(FakeComment(
            id=150, body="please also handle empty input",
            user=FakeUser("alice"),
        ))
        gh.add_pr(pr)
        gh.seed_state(
            1300,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=100,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-1300",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Fresh feedback wins over the drift check: the dev is NOT
        # spawned by `_handle_in_review`; the issue routes to `fixing`
        # with a bookmark covering BOTH surfaces (max across the
        # IssueComment id space).
        mocks["run_agent"].assert_not_called()
        self.assertIn((1300, "fixing"), gh.label_history)
        data = gh.pinned_data(1300)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 200)
        # Watermark stays at the seeded value so the future real fix
        # handler can re-scan both surfaces from there and find both
        # comments.
        self.assertEqual(data.get("pr_last_comment_id"), 100)

    def test_pr_conv_comment_above_issue_max_also_bookmarked(
        self,
    ) -> None:
        # Symmetric guard: a PR-conversation comment whose id is HIGHER
        # than every issue-thread id is still picked up by the
        # fresh-feedback scan (it surfaces in `pr_conversation_comments_after`
        # past the IssueComment-space watermark).
        gh = FakeGitHubClient()
        issue = make_issue(1310, label="in_review", body="updated body")
        gh.add_issue(issue)
        pr = FakePR(number=13100, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-1310")
        pr.issue_comments.append(FakeComment(
            id=600, body="additional ask",
            user=FakeUser("alice"),
        ))
        gh.add_pr(pr)
        gh.seed_state(
            1310,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=100,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-1310",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertIn((1310, "fixing"), gh.label_history)
        data = gh.pinned_data(1310)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 600)
