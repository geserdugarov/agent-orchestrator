# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import branch_publication, config, workflow

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
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleValidatingFreshReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=11,
            branch="orchestrator/issue-5",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(5, **defaults)
        return gh, issue

    def test_approved_flips_label_and_does_not_resume(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        # Approval routes through `documenting` for the final docs pass
        # before in_review picks up.
        self.assertIn((5, "documenting"), gh.label_history)
        self.assertNotIn((5, "in_review"), gh.label_history)
        self.assertTrue(any(
            ":white_check_mark: codex review approved" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_changes_requested_resumes_dev_increments_round(self) -> None:
        gh, issue = self._seeded()
        review = _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            # 1: before_sha for the dev-fix run. 2: after_sha to confirm
            # the new commit.
            head_shas=["aaa", "bbb"],
        )

        self.assertEqual(mocks["run_agent"].call_count, 2)
        # Second call (dev fix) must resume the developer session.
        _, second_kwargs = mocks["run_agent"].call_args_list[1]
        self.assertEqual(second_kwargs.get("resume_session_id"), "dev-sess")

        self.assertTrue(any(
            ":eyes: codex review (round 1/" in body and "Fix typo" in body
            for _, body in gh.posted_pr_comments
        ))
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.pinned_data(5).get("review_round"), 1)
        # Pushed dev fix stays on `validating` (no documenting hop) so
        # the reviewer re-evaluates the new head on the next tick. The
        # docs pass only runs as the final-docs handoff after approval.
        self.assertNotIn((5, "documenting"), gh.label_history)
        self.assertNotIn((5, "in_review"), gh.label_history)

    def test_unknown_verdict_parks_with_quoted_message(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                last_message="I'm not sure what to think",
                stderr="some subprocess noise",
            ),
        )

        self.assertTrue(gh.pinned_data(5).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertIn("> I'm not sure what to think", last_comment)
        # Real reviewer text is present, so the operator does not need
        # subprocess stderr in addition -- skip the diagnostic block.
        self.assertNotIn("Reviewer stderr", last_comment)
        # Label stays validating: no in_review transition.
        self.assertNotIn((5, "in_review"), gh.label_history)

    def test_empty_review_park_surfaces_stderr_and_exit_code(self) -> None:
        # Codex hit a Cloudflare interstitial: the agent exited with
        # nothing on stdout but the CF blob landed on stderr (#36). The
        # park comment must carry that tail so the operator can
        # distinguish CF / quota / auth from a true silent review.
        gh, issue = self._seeded()
        cf_blob = (
            "cf_chl_opt … Enable JavaScript and cookies to continue. "
            "Verifying you are human. This may take a few seconds."
        )
        with self.assertLogs("orchestrator.workflow", level="WARNING") as logs:
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    last_message="",
                    stderr=cf_blob,
                    exit_code=2,
                ),
            )

        self.assertTrue(gh.pinned_data(5).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertIn("(reviewer produced no final message)", last_comment)
        self.assertIn("_Reviewer stderr (last 1KB):_", last_comment)
        self.assertIn("Enable JavaScript and cookies", last_comment)
        self.assertIn("_Reviewer exit code:_ 2", last_comment)
        # Same data flowed to a WARNING log so operators tailing the
        # orchestrator log don't have to read GitHub to triage.
        self.assertTrue(any(
            "reviewer emitted no VERDICT" in r.getMessage()
            and "exit_code=2" in r.getMessage()
            for r in logs.records
        ))

    def test_empty_review_park_truncates_long_stderr(self) -> None:
        # A multi-MB CF response must not bloat the issue body. The
        # park comment caps stderr at 1KB.
        gh, issue = self._seeded()
        huge = "X" * 8192 + "TAIL_MARKER"
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", stderr=huge, exit_code=1),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("TAIL_MARKER", last_comment)
        # The leading head of the noise must be dropped by the cap.
        self.assertNotIn("X" * 4096, last_comment)

    def test_empty_review_park_with_no_stderr_omits_block(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", stderr=""),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertNotIn("_Reviewer stderr", last_comment)
        self.assertNotIn("_Reviewer exit code:_", last_comment)

    def test_reviewer_timeout_parks(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
        )

        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        # Tagged transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        self.assertEqual(data.get("park_reason"), "reviewer_timeout")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("reviewer timed out", last_comment)
        self.assertNotIn((5, "in_review"), gh.label_history)

    def test_reviewer_silent_crash_parks_with_reviewer_failed_reason(self) -> None:
        # The reviewer agent crashed (e.g. codex returned `Error: No such
        # file or directory (os error 2)`): empty last_message + non-zero
        # exit code. Tag the park as `reviewer_failed` so the next tick's
        # transient-recovery branch re-spawns the reviewer silently
        # without needing a human comment.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", stderr="boom", exit_code=2),
        )

        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "reviewer_failed")

    def test_reviewer_unknown_verdict_with_text_does_not_tag_failed(self) -> None:
        # When the reviewer DID emit text but no VERDICT line, the park
        # is real adjudication and must NOT be silently retried -- a
        # human needs to read the message. Park reason stays cleared.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                last_message="not sure what to think", exit_code=0,
            ),
        )

        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_reviewer_empty_message_with_zero_exit_does_not_tag_failed(self) -> None:
        # Defensive: empty last_message but exit_code == 0 is not a
        # crash -- the agent reported success without producing output.
        # Don't tag transient; a clean exit with no text needs human
        # adjudication, not a silent retry that would loop the same way.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", stderr="", exit_code=0),
        )

        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))


class HandleValidatingFixLoopEdgeCasesTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(6, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=12,
            branch="orchestrator/issue-6",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(6, **defaults)
        return gh, issue

    def _changes_requested_review(self):
        return _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )

    def test_dev_fix_timeout_parks_with_agent_timeout_reason(self) -> None:
        # The dev agent timed out mid-fix. The park must be tagged so the
        # next tick's recovery branch can rerun the reviewer instead of
        # waiting for a human comment that the timeout itself cannot
        # produce. The pre-agent SHA must also be persisted so recovery
        # can tell whether the agent committed before timing out (the
        # naive `_has_new_commits()` check is unconditionally true for a
        # PR worktree past its first fix).
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", timed_out=True),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa"],
        )

        data = gh.pinned_data(6)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        # `head_shas` are consumed in order: before_sha is "aaa", which
        # is what gets persisted.
        self.assertEqual(data.get("pre_dev_fix_sha"), "aaa")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent timed out", last_comment)

    def test_dev_fix_no_new_commit_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="why?"),
            ],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha (both "aaa" -> no commit).
            head_shas=["aaa", "aaa"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent needs your input", last_comment)

    def test_dev_fix_dirty_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="partial"),
            ],
            dirty_files=["leftover.py"],
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("leftover.py", last_comment)

    def test_dev_fix_push_fail_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="fixed"),
            ],
            dirty_files=(),
            push_branch=False,
            head_shas=["aaa", "bbb"],
        )

        data = gh.pinned_data(6)
        self.assertEqual(data.get("review_round"), 0)
        self.assertTrue(data.get("awaiting_human"))
        # The transient `push_failed` tag is what lets the next tick's
        # recovery branch silently retry the push without needing a human
        # comment to unstick the issue.
        self.assertEqual(data.get("park_reason"), "push_failed")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)

    def test_review_round_at_cap_parks_without_spawning_reviewer(self) -> None:
        gh, issue = self._seeded(review_round=config.MAX_REVIEW_ROUNDS)
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("review still has comments", last_comment)


class HandleValidatingAwaitingHumanResumeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_human_reply_resumes_dev_bumps_round_no_reviewer_this_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label="validating")
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite please", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            7,
            awaiting_human=True,
            last_action_comment_id=950,
            codex_session_id="dev-sess",
            review_round=1,
            pr_number=13,
            branch="orchestrator/issue-7",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Only the dev resume runs this tick; the reviewer fires on the next.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        followup = call.args[1]
        self.assertIn("use sqlite please", followup)

        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 2)
        # A successful awaiting-human resume stays on `validating` (no
        # documenting hop) so the reviewer re-runs against the new head
        # on the next tick.
        self.assertNotIn((7, "documenting"), gh.label_history)
        self.assertNotIn((7, "in_review"), gh.label_history)

    def test_successful_dev_fix_resets_silent_park_streak(self) -> None:
        # The validating / in_review fix paths exit on `_handle_dev_fix_result`
        # returning True without going through `_on_commits`. Without an
        # explicit reset on that branch, `silent_park_count` would still
        # carry over from earlier silent parks, and a later single empty
        # resume could tip an otherwise-healthy session past the
        # fresh-session threshold.
        gh = FakeGitHubClient()
        issue = make_issue(70, label="validating")
        issue.comments.append(
            FakeComment(id=1100, body="please fix it", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            70,
            awaiting_human=True,
            last_action_comment_id=950,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            pr_number=14,
            branch="orchestrator/issue-70",
            # Carryover from an earlier silent park; one short of the
            # fresh-session threshold.
            silent_park_count=1,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        data = gh.pinned_data(70)
        self.assertEqual(
            data.get("silent_park_count"), 0,
            "a successful dev fix must reset the silent-park streak so a "
            "later transient empty result doesn't drop a healthy session",
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
            branch=f"orchestrator/issue-{issue_number}",
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


class HandleValidatingReviewCapAddRoundsCommandTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`/orchestrator add-review-rounds N` operator command.

    Honored only while parked with `park_reason == "review_cap"`. Resets
    `review_round` to `MAX_REVIEW_ROUNDS - N` so the reviewer reruns from
    validating without losing the PR/worktree. Posting a plain reply on a
    cap park no longer wakes the dev session (that was the original bug:
    the resume just bumped past the cap again on the next tick).
    """

    def _seeded(self, *, comment_body: Optional[str] = None, **state):
        gh = FakeGitHubClient()
        issue = make_issue(80, label="validating")
        if comment_body is not None:
            issue.comments.append(
                FakeComment(id=1100, body=comment_body, user=FakeUser("alice"))
            )
        gh.add_issue(issue)
        defaults = dict(
            awaiting_human=True,
            park_reason="review_cap",
            last_action_comment_id=950,
            review_round=config.MAX_REVIEW_ROUNDS,
            dev_session_id="dev-sess",
            dev_agent="codex",
            pr_number=15,
            branch="orchestrator/issue-80",
        )
        defaults.update(state)
        gh.seed_state(80, **defaults)
        return gh, issue

    def test_command_resets_round_clears_park_and_reruns_reviewer(self) -> None:
        # Granting 1 more round on a 3-cap means review_round becomes 2.
        # The reviewer-spawn block fires on the SAME tick (fall-through
        # parity with the reviewer_timeout / reviewer_failed branches) so
        # the operator does not have to wait an extra poll for the
        # reviewer to actually rerun.
        gh, issue = self._seeded(
            comment_body="/orchestrator add-review-rounds 1",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                last_message="LGTM\n\nVERDICT: APPROVED",
            ),
            head_shas=["aaa"],
        )

        data = gh.pinned_data(80)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(
            data.get("review_round"),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        # Watermark advanced past the operator's command comment so the
        # next tick doesn't re-fire the same command.
        self.assertEqual(data.get("last_action_comment_id"), 1100)
        # Reviewer ran THIS tick (parity with reviewer_timeout fall-through).
        self.assertEqual(mocks["run_agent"].call_count, 1)
        reviewer_spawns = [
            e for e in gh.recorded_events
            if e["event"] == "agent_spawn"
            and e.get("agent_role") == "reviewer"
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(
            reviewer_spawns[0]["review_round"],
            config.MAX_REVIEW_ROUNDS - 1,
        )
        # Confirmation comment posted on the issue.
        self.assertTrue(any(
            "review-cap reset" in body and "granting 1 more round" in body
            for _, body in gh.posted_comments
        ))

    def test_command_grants_full_reset_when_n_meets_or_exceeds_max(
        self,
    ) -> None:
        # `N >= MAX_REVIEW_ROUNDS` clamps review_round to 0 -- the full
        # reset. The reviewer-spawn block then runs with a fresh budget.
        gh, issue = self._seeded(
            comment_body=(
                f"/orchestrator add-review-rounds {config.MAX_REVIEW_ROUNDS + 5}"
            ),
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=["aaa"],
        )

        self.assertEqual(gh.pinned_data(80).get("review_round"), 0)

    def test_command_picks_latest_when_multiple_present(self) -> None:
        # Two commands in the same batch: the later one wins so a
        # corrected post supersedes a stale typo without needing the
        # operator to delete the first comment.
        gh, issue = self._seeded()
        issue.comments.append(
            FakeComment(
                id=1100,
                body="/orchestrator add-review-rounds 1",
                user=FakeUser("alice"),
            )
        )
        issue.comments.append(
            FakeComment(
                id=1101,
                body="actually scratch that\n"
                "/orchestrator add-review-rounds 2",
                user=FakeUser("alice"),
            )
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=["aaa"],
        )

        self.assertEqual(
            gh.pinned_data(80).get("review_round"),
            config.MAX_REVIEW_ROUNDS - 2,
        )
        self.assertEqual(gh.pinned_data(80).get("last_action_comment_id"), 1101)

    def test_command_with_zero_is_rejected_stays_parked(self) -> None:
        gh, issue = self._seeded(
            comment_body="/orchestrator add-review-rounds 0",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No agent ran: the error path stays parked, doesn't fall through.
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(80)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "review_cap")
        # Round is unchanged.
        self.assertEqual(
            data.get("review_round"), config.MAX_REVIEW_ROUNDS,
        )
        # Watermark advanced so the operator can post a corrected command
        # in a new comment without re-tripping the same rejection.
        self.assertEqual(data.get("last_action_comment_id"), 1100)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("ignored", last_comment)
        self.assertIn("positive integer", last_comment)

    def test_plain_human_reply_stays_parked_no_dev_resume(self) -> None:
        # The original bug: on a `review_cap` park, a plain human reply
        # used to wake the dev session and the reviewer rebumped past
        # the cap on the next tick. The new behavior is to stay parked
        # silently when no command is present; only the explicit command
        # can restart the loop.
        gh, issue = self._seeded(comment_body="any luck on this?")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(80)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "review_cap")
        # Watermark NOT advanced -- the operator may still post the
        # command later in a follow-up comment, and we need to see it.
        self.assertEqual(data.get("last_action_comment_id"), 950)

    def test_command_only_fires_on_review_cap_park(self) -> None:
        # A command posted under a different park reason (here: a
        # standard dev-question park with `park_reason=None`) must NOT
        # take the cap-reset branch. The dev resume runs as usual.
        gh, issue = self._seeded(
            comment_body="/orchestrator add-review-rounds 1",
            park_reason=None,
            review_round=1,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="fixed"),
            head_shas=["aaa", "bbb"],
            dirty_files=(),
            push_branch=True,
        )

        data = gh.pinned_data(80)
        # Dev resume bumped the round; no cap-reset semantics applied.
        self.assertEqual(data.get("review_round"), 2)
        # No reset confirmation comment was posted.
        self.assertFalse(any(
            "review-cap reset" in body
            for _, body in gh.posted_comments
        ))

    def test_command_inline_in_prose_does_not_fire(self) -> None:
        # The regex requires the command at the start of a line, so a
        # quote of the syntax in regular prose (e.g. the operator asking
        # someone else how to use it) does not trigger the reset.
        gh, issue = self._seeded(
            comment_body=(
                "do we just run `/orchestrator add-review-rounds 1` here?"
            ),
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(80)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "review_cap")
        self.assertEqual(
            data.get("review_round"), config.MAX_REVIEW_ROUNDS,
        )

    def test_review_cap_park_message_advertises_command(self) -> None:
        # When the orchestrator first parks on the cap, the park comment
        # itself surfaces the command so an operator who has never seen
        # the syntax can copy/paste it from the issue thread.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            81,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=16,
            branch="orchestrator/issue-81",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("/orchestrator add-review-rounds", last_comment)

    def test_cap_park_persists_park_reason_for_next_tick(self) -> None:
        # `_park_awaiting_human` always clears `park_reason` to None (its
        # `reason=` kwarg only feeds the audit event), so the cap branch
        # must re-set the durable field itself. Without this, the next
        # tick's awaiting-human dispatch sees `park_reason=None` and the
        # `/orchestrator add-review-rounds` parser never runs -- the
        # command would silently fall through to the dev-resume branch.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            82,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=17,
            branch="orchestrator/issue-82",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        data = gh.pinned_data(82)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "review_cap")

    def test_command_fires_after_real_cap_park_two_ticks(self) -> None:
        # End-to-end regression for the original bug: the FIRST tick must
        # park via the cap branch (not pre-seeded shortcut), persist
        # `park_reason="review_cap"`, and seed a `user_content_hash`. The
        # SECOND tick must then bypass the user-content-drift branch
        # (the operator's command comment changes the hash by definition)
        # and route through the cap-reset path so the round actually
        # resets. Pre-seeded tests above cover the command parser in
        # isolation; this one closes the loop on the production sequence.
        gh = FakeGitHubClient()
        issue = make_issue(83, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            83,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=18,
            branch="orchestrator/issue-83",
            pickup_comment_id=900,
            dev_session_id="dev-sess",
            dev_agent="codex",
        )

        # Tick 1: cap park.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        tick1 = gh.pinned_data(83)
        self.assertTrue(tick1.get("awaiting_human"))
        self.assertEqual(tick1.get("park_reason"), "review_cap")
        # The user-content baseline got seeded on the cap tick (either
        # by the drift helper's first-call branch or via the orchestrator's
        # own park comment routing). Either way the next tick has a hash
        # to compare against.
        self.assertIsInstance(tick1.get("user_content_hash"), str)
        baseline_hash = tick1["user_content_hash"]

        # Operator posts the command after the cap park. This is a
        # non-orchestrator comment, so it shifts the content hash --
        # without the drift-block bypass the next tick would resume the
        # dev session on a body-edit prompt and never see the command.
        issue.comments.append(
            FakeComment(
                id=2000,
                body="/orchestrator add-review-rounds 1",
                user=FakeUser("alice"),
            )
        )

        # Tick 2: command processes through the cap-reset path.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=["aaa"],
        )
        tick2 = gh.pinned_data(83)
        self.assertFalse(tick2.get("awaiting_human"))
        self.assertIsNone(tick2.get("park_reason"))
        self.assertEqual(
            tick2.get("review_round"), config.MAX_REVIEW_ROUNDS - 1,
        )
        self.assertEqual(tick2.get("last_action_comment_id"), 2000)
        # The drift block updates the baseline as it falls through, so
        # the new hash should be persisted -- but the resumed-dev-session
        # drift message must NOT have been posted.
        self.assertNotEqual(tick2.get("user_content_hash"), baseline_hash)
        self.assertFalse(any(
            "issue body changed; resuming dev session" in body
            for _, body in gh.posted_comments
        ))
        # The cap-reset confirmation landed AND the reviewer ran with
        # the freshly-reset round.
        self.assertTrue(any(
            "review-cap reset" in body for _, body in gh.posted_comments
        ))
        reviewer_spawns = [
            e for e in gh.recorded_events
            if e["event"] == "agent_spawn"
            and e.get("agent_role") == "reviewer"
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(
            reviewer_spawns[0]["review_round"],
            config.MAX_REVIEW_ROUNDS - 1,
        )


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
    BRANCH = "orchestrator/issue-5"

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
            number=50, head_branch="orchestrator/issue-99",
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
            branch="orchestrator/issue-99",
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


class SquashOnApprovalTest(unittest.TestCase, _PatchedWorkflowMixin):
    """After the reviewer agent emits VERDICT: APPROVED, the orchestrator
    squashes the dev's commits on the PR branch into one and force-pushes
    so the resulting PR is a single conventional-commit-shaped commit.
    Watermarks advance past the squash notice; the next in_review tick
    pings HITL without re-running the reviewer on the rewritten head.

    Failures (push rejected, lease violation, dirty tree) park
    awaiting_human and leave the original commits in place; SQUASH_ON_APPROVAL
    off preserves the legacy "leave the dev's commits as-is" behavior.
    """

    PR_NUMBER = 31
    BRANCH = "orchestrator/issue-5"
    REVIEWED_SHA = "reviewedAA"
    SQUASHED_SHA = "squashedBB"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(5, label="validating", title="add a feature", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #31",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        # PR head SHA mirrors the post-squash remote head -- the force-push
        # inside the squash helper updates the remote, so by the time the
        # next gh.get_pr() is taken (inside _handle_validating's seeding
        # block, AND on the next in_review tick) the remote head matches
        # the new local SHA.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha=self.SQUASHED_SHA),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            5,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_approval_squashes_and_lands_in_review_without_re_review(
        self,
    ) -> None:
        # End-to-end: validating approves, squash + force-push runs (mocked
        # to succeed), the squash PR comment is posted, the issue lands in
        # in_review, and the next in_review tick pings HITL WITHOUT
        # spawning the reviewer on the rewritten head.
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            mocks_v = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                # Squash: success, new local HEAD = SQUASHED_SHA, 3 commits
                # collapsed to 1.
                squash_result=(True, self.SQUASHED_SHA, 3, None),
            )

        # Squash helper was called exactly once on the approval path.
        self.assertEqual(mocks_v["_squash_and_force_push"].call_count, 1)
        # Reviewer ran once -- the only run_agent call on the approval path.
        self.assertEqual(mocks_v["run_agent"].call_count, 1)
        # Approval hands off through `documenting` (final docs pass);
        # `_handle_documenting`'s success exits advance unconditionally to
        # `in_review`. The squash / watermark state rides through the hop
        # untouched.
        self.assertIn((5, "documenting"), gh.label_history)
        data = gh.pinned_data(5)
        # The squash notice was posted to the PR conversation.
        squash_notice_posted = any(
            ":package: squashed 3 commits to 1" in body
            for _, body in gh.posted_pr_comments
        )
        self.assertTrue(
            squash_notice_posted,
            f"squash notice not posted; got: {gh.posted_pr_comments}",
        )
        # Watermark must include the squash comment so the next in_review
        # tick does not see it as fresh PR feedback once debounce expires.
        approval_and_squash_ids = [c.id for c in pr.issue_comments]
        self.assertTrue(approval_and_squash_ids)
        self.assertGreaterEqual(
            data.get("pr_last_comment_id"), max(approval_and_squash_ids),
            "pr_last_comment_id must advance past both the approval and "
            "the squash PR comments",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # Approved + mergeable; the ping MUST fire and must NOT re-run
        # the reviewer agent (its run_agent call would otherwise be
        # visible in mocks_r below).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for c in list(issue.comments) + list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks_r = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks_r["run_agent"].assert_not_called()
        # The orchestrator is manual-merge-only: the post-squash head
        # earns a HITL ping for the human to merge by hand. No
        # orchestrator-initiated merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((5, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(5).get("ready_ping_sha"), self.SQUASHED_SHA,
        )

    def test_squash_failure_parks_awaiting_human_without_relabel(self) -> None:
        # Push rejected / lease violation / dirty tree all surface as
        # `success=False`. The orchestrator parks awaiting_human, leaves
        # the issue in `validating`, and does NOT seed watermarks (the
        # original commits remain on the branch and a human can decide
        # what to do).
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                squash_result=(
                    False, None, 0,
                    "force-push with lease rejected (concurrent update)",
                ),
            )

        self.assertEqual(mocks["_squash_and_force_push"].call_count, 1)
        # Park happened: awaiting_human flag set, HITL message posted to
        # the issue thread.
        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        park_posted = any(
            "squash-on-approval failed" in body
            for _, body in gh.posted_comments
        )
        self.assertTrue(
            park_posted,
            f"HITL park message not posted; got: {gh.posted_comments}",
        )
        # No relabel to in_review or documenting -- the issue stays in
        # `validating` so the original commits remain on the branch.
        self.assertNotIn(
            (5, "in_review"), gh.label_history,
            "park must NOT relabel to in_review on squash failure",
        )
        self.assertNotIn(
            (5, "documenting"), gh.label_history,
            "park must NOT relabel to documenting (the final-docs hop) "
            "on squash failure",
        )

    def test_squash_off_preserves_legacy_behavior(self) -> None:
        # Kill switch: with SQUASH_ON_APPROVAL=off the squash helper must
        # NOT be called and no squash notice is posted.
        gh, issue, pr = self._setup()
        # Make pr.head.sha match REVIEWED_SHA -- legacy path: the local
        # HEAD the reviewer saw is what the remote PR points at, since no
        # force-push happened.
        pr.head = FakePRRef(sha=self.REVIEWED_SHA)

        with patch.object(config, "SQUASH_ON_APPROVAL", False):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
            )

        # Helper not called at all.
        mocks["_squash_and_force_push"].assert_not_called()
        # No squash notice posted.
        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # And the legacy approval flow flips to `documenting` (the
        # final-docs hop) regardless of SQUASH_ON_APPROVAL.
        self.assertIn((5, "documenting"), gh.label_history)

    def test_squash_with_only_one_commit_does_not_post_notice(self) -> None:
        # The helper returns `squashed_count=0` when there's only one
        # commit on top of base -- nothing to squash. The orchestrator
        # must skip the squash PR comment (the helper returns the same
        # SHA back).
        gh, issue, pr = self._setup()
        pr.head = FakePRRef(sha=self.REVIEWED_SHA)

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                # Helper success no-op: nothing to squash.
                squash_result=(True, self.REVIEWED_SHA, 0, None),
            )

        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # Approval still flips to `documenting` (the final-docs hop)
        # even when there's only one commit (so no squash notice).
        self.assertIn((5, "documenting"), gh.label_history)


class SquashHelperRealGitTest(unittest.TestCase):
    """Integration test for `_squash_and_force_push` against a real git repo.

    The workflow-level squash tests above mock the helper itself, so they
    cannot catch failures in its rollback logic, in the squash-commit
    message construction, or in the lease-pinning. This class creates a
    bare remote + working clone with multiple commits on a topic branch,
    runs the helper directly, and asserts the on-disk state.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-squash-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        # Bare remote + working clone, base branch "main".
        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        # Identity for prep commits below; the orchestrator-owned squash
        # commit uses its own GIT_AUTHOR_*/GIT_COMMITTER_* env vars, so
        # this is just for the dev's pre-squash commits.
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        # Initial commit on main.
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Topic branch with three dev commits.
        self.branch = "orchestrator/issue-9"
        self._git("checkout", "-b", self.branch, cwd=self.work)
        for i, msg in enumerate(["fix: typo", "add foo", "add bar"], start=1):
            (self.work / f"f{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )
        self._git("push", "origin", self.branch, cwd=self.work)
        self._git("fetch", "origin", cwd=self.work)

    def _make_issue(self, title: str = "test issue", number: int = 9):
        return make_issue(number, title=title)

    def _commits_on_branch(self) -> list[str]:
        """Subjects of all commits between origin/main and HEAD, oldest first."""
        out = self._git(
            "log", "--reverse", "--pretty=%s", "origin/main..HEAD",
            cwd=self.work,
        )
        return [s for s in out.splitlines() if s.strip()]

    def test_squash_collapses_three_commits_to_one(self) -> None:
        # First commit's subject ("fix: typo") is conventional-commit form,
        # so the squash subject reuses it; body lists all three.
        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, new_sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, f"expected success, got err={err!r}")
        self.assertIsNone(err)
        self.assertEqual(count, 3)
        self.assertTrue(new_sha)

        commits = self._commits_on_branch()
        self.assertEqual(
            len(commits), 1,
            f"expected one commit on top of base, got {commits!r}",
        )
        # Squash subject reuses the conventional-commit first subject.
        self.assertEqual(commits[0], "fix: typo")
        # Body aggregates all original subjects.
        body = self._git(
            "log", "-1", "--pretty=%B", cwd=self.work,
        )
        self.assertIn("Squashed commits:", body)
        for original in ("- fix: typo", "- add foo", "- add bar"):
            self.assertIn(original, body)

    def test_squash_uses_issue_title_when_no_conventional_first_subject(
        self,
    ) -> None:
        # Reset and rebuild the branch with non-conv-commit first subject.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        for i, msg in enumerate(["typo fix", "feat: add foo"], start=1):
            (self.work / f"g{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )

        issue = self._make_issue(title="rename frobnicator")
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)

        subject = self._git("log", "-1", "--pretty=%s", cwd=self.work).strip()
        self.assertEqual(subject, "feat: rename frobnicator")

    def test_squash_with_only_one_commit_is_a_no_op(self) -> None:
        # Reset to a single commit on top of base.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "only.txt").write_text("only\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "feat: only one", cwd=self.work,
            env_extra=author_env,
        )
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()

        issue = self._make_issue()
        push_mock = patch.object(branch_publication, "_push_branch", return_value=True)
        with patch.object(config, "BASE_BRANCH", "main"), push_mock as pm:
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success)
        self.assertEqual(count, 0)
        self.assertEqual(sha, original_head)
        # Single-commit branch must NOT trigger a push at all.
        pm.assert_not_called()
        # HEAD unchanged.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )

    def test_rollback_restores_branch_when_force_push_fails(self) -> None:
        # The whole point of saving original_head: a push failure after
        # the soft-reset + squash commit must not leave the branch
        # pointing at the squash commit. The original commits must still
        # be on the branch so the operator can decide what to do.
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=False):
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("force-push", err or "")
        # HEAD restored.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        # All three original commits still on the branch.
        self.assertEqual(self._commits_on_branch(), original_subjects)
        # Working tree clean (rollback used --hard, but pre-reset tree
        # already matched HEAD's tree, so no file diffs should remain).
        status = self._git("status", "--porcelain", cwd=self.work)
        self.assertEqual(status.strip(), "")

    def test_squash_commit_uses_orchestrator_identity(self) -> None:
        # The squash commit must be authored under AGENT_GIT_NAME /
        # AGENT_GIT_EMAIL regardless of the dev's commit identity. This
        # keeps a single attribution for orchestrator-owned commits and
        # matches the agent-spawn `_agent_env` behavior.
        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True), \
             patch.object(config, "AGENT_GIT_NAME", "orch-bot"), \
             patch.object(
                 config, "AGENT_GIT_EMAIL", "orch-bot@example.com"
             ):
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)

        author = self._git(
            "log", "-1", "--pretty=%an <%ae>", cwd=self.work,
        ).strip()
        committer = self._git(
            "log", "-1", "--pretty=%cn <%ce>", cwd=self.work,
        ).strip()
        self.assertEqual(author, "orch-bot <orch-bot@example.com>")
        self.assertEqual(committer, "orch-bot <orch-bot@example.com>")

    def test_dirty_worktree_aborts_before_reset(self) -> None:
        # An uncommitted change in the worktree (the agent left work
        # behind) is a refuse-to-rewrite signal: the helper must abort
        # WITHOUT touching HEAD so the dirty state is visible to the
        # operator. Without the pre-reset dirty check the soft-reset
        # would happen and the rollback would clobber the dirty changes.
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        (self.work / "scratch.txt").write_text("uncommitted\n")

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True) as pm:
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIn("uncommitted", (err or ""))
        # HEAD untouched, dirty file preserved, no push attempted.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / "scratch.txt").exists())
        pm.assert_not_called()

    def test_dirty_worktree_with_single_commit_still_fails(self) -> None:
        # The dirty-tree refusal is a precondition for the whole helper,
        # not just the rewrite path. A one-commit branch (squash would
        # be a no-op) with an uncommitted file must still fail so the
        # caller parks awaiting_human; otherwise the manual merge could
        # land the head with the operator's scratch invisible on the PR.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "only.txt").write_text("only\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "feat: only one", cwd=self.work,
            env_extra=author_env,
        )
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        (self.work / "scratch.txt").write_text("uncommitted\n")

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True) as pm:
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("uncommitted", (err or ""))
        # Single-commit + dirty path must NOT short-circuit to the
        # no-op success branch. HEAD untouched, dirty file preserved,
        # no push attempted.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / "scratch.txt").exists())
        pm.assert_not_called()


class ValidatingHandoffPreservesHumanFeedbackTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human review comment posted while validating is still running must
    not be silently consumed when the validating handler approves and seeds
    the in_review watermarks. Otherwise the dev would never see the
    human's feedback before in_review pings HITL for the manual merge.
    """

    PR_NUMBER = 22
    BRANCH = "orchestrator/issue-15"

    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(15, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #22",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            # Human posted a review comment during validating, BEFORE the
            # orchestrator's approval comment lands. Without the watermark
            # fix, the validating handler would seed pr_last_comment_id past
            # this comment and the next in_review tick would never see it.
            issue_comments=[
                FakeComment(
                    id=950, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            15, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_pre_handoff_human_pr_comment_is_processed_in_in_review(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. The orchestrator's approval comment
        # lands AFTER the human's. With the fix, the watermark stops at
        # the first human comment instead of swallowing it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        # Validating's approval flips through `documenting` first (the
        # final-docs hop); the watermark must already be seeded past the
        # human's pre-handoff PR comment by the time the docs pass runs.
        self.assertIn((15, "documenting"), gh.label_history)
        wm = gh.pinned_data(15).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must stop before human comment id=950 (got {wm})",
        )

        # Step 2: in_review tick. The human comment is visible past the
        # watermark and the handler routes the issue to `fixing` (no dev
        # spawn here; the fixing handler drives the resume). Without the
        # surfacing, the handler would ping HITL for the manual merge
        # over the human's unaddressed feedback.
        from tests.fakes import FakeLabel
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # No merge happened; issue routed to `fixing` so the human's
        # feedback is owned by the fix loop.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((15, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(15).get("pending_fix_issue_max_id"), 950,
        )


class PrePickupChatterHandoffTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Pre-pickup human comments on the issue (the original discussion that
    landed in the dev agent's spawn context) must be advanced past at
    validating -> in_review handoff. If the watermark stops at the first
    non-self comment, those same already-consumed comments replay as fresh
    PR feedback once the in_review debounce expires -- a ready-for-merge
    candidate would instead bounce back through validating in a loop.
    """

    PR_NUMBER = 25
    BRANCH = "orchestrator/issue-20"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(20, label="validating", comments=[
            FakeComment(
                id=850,
                body="original issue clarification posted before pickup",
                user=FakeUser("alice"),
                created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #25",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            20, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_pickup_chatter_does_not_replay_at_in_review(self) -> None:
        gh, issue, pr, long_ago = self._setup()

        # Step 1: validating approves. Watermark must include id 850 so the
        # pre-pickup human comment is treated as consumed.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(20).get("pr_last_comment_id")
        self.assertIsNotNone(wm, "watermark must be seeded past pre-pickup")
        self.assertGreaterEqual(
            wm, 901,
            f"watermark must advance past pre-pickup chatter and self-run; "
            f"got {wm}",
        )

        # Backdate the approval comment too so debounce wouldn't filter it
        # out as a confound (it shouldn't matter because the watermark
        # already covers it, but be explicit).
        for c in list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago

        # Step 2: in_review tick. With the fix, no comment is past the
        # watermark, so the handler reaches the mergeable / HITL-ping
        # path. Without the fix, the human comment id=850 surfaces as
        # "new" and the issue routes to `fixing`.
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Manual-merge-only: no orchestrator merge, but the HITL ping
        # fires because the watermark fix kept the pre-pickup chatter
        # out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((20, "done"), gh.label_history)
        self.assertNotIn((20, "fixing"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)


class ValidatingTransientParkRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A validating-side park whose underlying condition can self-resolve
    (a non-fast-forward push that the next --force-with-lease push will
    land) must auto-recover without needing a fresh issue-thread comment.
    Otherwise `_resume_developer_on_human_reply` -- which only fires on a
    new comment -- leaves the issue parked indefinitely even after the
    transient cause is gone.
    """

    BRANCH = "orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str, **extra_state):
        gh = FakeGitHubClient()
        # `last_action_comment_id` is well above any existing comment id, so
        # `comments_after` returns []. This mirrors the post-park watermark
        # set by `_park_awaiting_human` (it bumps to the latest comment id).
        issue = make_issue(170, label="validating")
        gh.add_issue(issue)
        seed = dict(
            pr_number=99, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=1,
            awaiting_human=True,
            park_reason=park_reason,
            last_action_comment_id=10_000,
        )
        seed.update(extra_state)
        gh.seed_state(170, **seed)
        return gh, issue

    def test_push_failed_park_recovers_when_push_succeeds(self) -> None:
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Force the worktree-existence check to pass; "/tmp" always exists
        # on Linux. The recovery only retries the push when the worktree
        # is still on disk (otherwise the dev's local commits are gone and
        # only a human relabel can unstick the issue).
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery must NOT spawn the agent or post any comment -- it is a
        # silent retry.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Push retried and succeeded: park flags cleared, review_round
        # incremented so the next reviewer run starts a fresh round.
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 2)
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertEqual(gh.label_history, [])
        self.assertNotIn((170, "documenting"), gh.label_history)
        self.assertNotIn((170, "in_review"), gh.label_history)

    def test_push_failed_park_stays_parked_when_push_still_fails(self) -> None:
        # Recovery must not re-post the park message when the push still
        # fails -- otherwise every poll would spam the issue.
        gh, issue = self._parked_issue(park_reason="push_failed")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=False,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")
        # review_round NOT bumped while still stuck.
        self.assertEqual(data.get("review_round"), 1)

    def test_push_failed_park_stays_parked_when_worktree_is_gone(self) -> None:
        # If the worktree was reaped between the original park and the
        # recovery tick, the dev's local commits are gone and there is
        # nothing to push. Stay parked so a human can intervene.
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Path that will not exist on the test host.
        gone = Path("/tmp/orchestrator-test-recovery-no-such-worktree-xyz")
        with patch.object(workflow, "_worktree_path", return_value=gone):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")

    def test_non_transient_park_stays_parked_with_no_new_comments(self) -> None:
        # A park whose reason is not in the validating transient set (e.g.
        # a question or dirty-tree park) must NOT auto-recover. The
        # _resume_developer_on_human_reply path (no new comments) returns
        # without doing anything; recovery is the only other path and it
        # bails on park_reason.
        gh, issue = self._parked_issue(park_reason=None)

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_timeout_park_recovers_silently(self) -> None:
        # A previous tick parked because the reviewer agent timed out.
        # The next tick must clear the flags so the reviewer re-runs --
        # nothing in `_resume_developer_on_human_reply` would unstick this
        # otherwise (no comment ever lands from a timeout).
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery is silent on this tick: the agent is NOT re-spawned
        # here (next tick does that, on the cleared awaiting_human flag),
        # no push is attempted (no fix landed), and no new comment is
        # posted.
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # review_round MUST NOT advance: a timeout produced no fix, so
        # bumping would burn through MAX_REVIEW_ROUNDS without progress.
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_failed_park_recovers_silently(self) -> None:
        # The reviewer crashed with empty stdout + non-zero exit on the
        # previous tick. Recovery must clear the flags so the next tick
        # re-spawns the reviewer with a fresh budget -- without this,
        # the issue waits for a human comment that the codex / network
        # blip cannot produce.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # No fix landed; a reviewer crash produces no commit, so the
        # round must stay flat (mirrors the reviewer_timeout branch).
        self.assertEqual(data.get("review_round"), 1)

    def test_reviewer_failed_park_with_new_comment_routes_to_reviewer(self) -> None:
        # A human "Retry" / "Continue" nudge after a reviewer-side park
        # must wake the REVIEWER, not the dev. Pre-fix this branch fed
        # the comment to `_resume_developer_on_human_reply`, which woke
        # the dev session; the dev correctly answered "nothing to do,
        # the reviewer should re-run" and the issue wedged.
        gh, issue = self._parked_issue(park_reason="reviewer_failed")
        issue.comments.append(
            FakeComment(
                id=10_500, body="retry please",
                user=FakeUser("alice"),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message="LGTM\n\nVERDICT: APPROVED",
        )
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=review,
                head_shas=["cafe1234"],
            )

        # Exactly one agent ran: the reviewer (not the dev). The agent
        # call must use the reviewer config, not the dev session resume.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        # Park flags cleared and the human's comment is consumed so it
        # cannot replay on the next tick.
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("last_action_comment_id"), 10_500)

    def test_reviewer_timeout_park_with_new_comment_routes_to_reviewer(self) -> None:
        # Same routing rule for the reviewer_timeout park reason: a
        # human nudge must reach the reviewer, not the dev session.
        gh, issue = self._parked_issue(park_reason="reviewer_timeout")
        issue.comments.append(
            FakeComment(
                id=10_500, body="retry please",
                user=FakeUser("alice"),
            )
        )

        review = _agent(
            session_id="rev-sess",
            last_message="LGTM\n\nVERDICT: APPROVED",
        )
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=review,
                head_shas=["cafe1234"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], config.REVIEW_AGENT)
        self.assertNotIn("resume_session_id", call.kwargs)
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_agent_timeout_park_with_new_comment_still_routes_to_dev(self) -> None:
        # Regression: dev-side park reasons (agent_timeout) must keep
        # routing to the dev session on a human comment. Only
        # reviewer-side reasons get the new fall-through.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )
        issue.comments.append(
            FakeComment(
                id=10_500, body="please rebase first",
                user=FakeUser("alice"),
            )
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # The dev was resumed with the human's feedback (NOT the reviewer).
        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        followup = call.args[1]
        self.assertIn("please rebase first", followup)

    def test_agent_timeout_clean_tree_no_commits_recovers_silently(self) -> None:
        # Common timeout shape: the dev burned the budget without
        # producing a new commit. Recovery clears flags and does not
        # bump the round (no fix landed); next tick re-runs the reviewer.
        # `head_shas[0] == pre_dev_fix_sha` models "agent did nothing"
        # (worktree HEAD unchanged from the pre-agent watermark).
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 1)
        # Watermark cleared so a future timeout cycle starts fresh.
        self.assertIsNone(data.get("pre_dev_fix_sha"))

    def test_agent_timeout_existing_pr_commits_no_new_commit(self) -> None:
        # Regression: a normal PR worktree is always ahead of
        # `origin/<base>` after the first fix lands. `_has_new_commits()`
        # would say "yes" even when this run produced nothing, so naive
        # recovery would call `_push_branch()` (force-with-lease over
        # the live remote head with a stale local HEAD) and bump the
        # round on every tick. The pre/now SHA comparison must guard
        # against that.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                # Mock `_has_new_commits` to True to model an established
                # PR worktree (commits ahead of origin/main); the
                # recovery must not consult this signal.
                has_new_commits=True,
                dirty_files=(),
                push_branch=True,
                head_shas=("cafe1234",),  # HEAD == pre_dev_fix_sha
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # MUST NOT bump: nothing landed.
        self.assertEqual(data.get("review_round"), 1)

    def test_agent_timeout_with_unpushed_commits_pushes_and_bumps(self) -> None:
        # The dev committed the fix locally but the timeout killed it
        # before the push. Recovery must finish that push -- otherwise
        # the next tick's reviewer would inspect a SHA that is not on
        # the PR. `head_shas[0] != pre_dev_fix_sha` models "agent
        # produced a new commit before timing out."
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("beef5678",),  # HEAD moved past pre-agent SHA
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # Bumped: a real fix landed.
        self.assertEqual(data.get("review_round"), 2)
        self.assertIsNone(data.get("pre_dev_fix_sha"))
        # Stays on `validating` (no documenting hop) so the reviewer
        # re-evaluates the recovered head on the next tick.
        self.assertNotIn((170, "documenting"), gh.label_history)

    def test_agent_timeout_with_unpushed_commits_push_fails_stays_parked(
        self,
    ) -> None:
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=False,
                head_shas=("beef5678",),
            )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        # NOT bumped while still stuck; watermark preserved for next try.
        self.assertEqual(data.get("review_round"), 1)
        self.assertEqual(data.get("pre_dev_fix_sha"), "cafe1234")

    def test_agent_timeout_with_dirty_worktree_stays_parked(self) -> None:
        # The dev edited files without committing before timing out.
        # Recovery refuses to silently push (would publish an incomplete
        # branch) or to clear flags (the next reviewer would inspect
        # uncommitted state). Stays parked until a human or comment-
        # driven resume sorts the dirty edits out.
        gh, issue = self._parked_issue(
            park_reason="agent_timeout",
            pre_dev_fix_sha="cafe1234",
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=["leftover.py"],
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        # No new comment posted on this tick -- the original park
        # message still describes the situation.
        self.assertEqual(gh.posted_comments, [])
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")
        self.assertEqual(data.get("review_round"), 1)

    def test_agent_timeout_without_watermark_stays_parked(self) -> None:
        # Defensive: if the timeout park ran in foreign code that did
        # not persist `pre_dev_fix_sha`, recovery cannot tell whether a
        # commit was produced. Refuse to act -- a force-push of a stale
        # local HEAD would silently rewrite remote.
        gh, issue = self._parked_issue(park_reason="agent_timeout")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                dirty_files=(),
                push_branch=True,
                head_shas=("anything",),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_timeout")

    def test_transient_park_with_new_comment_takes_resume_path(self) -> None:
        # A transient park is preempted by a fresh human comment: the
        # comment-driven resume path wins, the dev is spawned with the
        # human's feedback, and the recovery branch does not silently
        # retry the push. This ensures the human's reply is not dropped.
        gh, issue = self._parked_issue(park_reason="push_failed")
        issue.comments.append(
            FakeComment(
                id=10_500, body="please rebase first",
                user=FakeUser("alice"),
            )
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev was resumed with the human's feedback (recovery did NOT run).
        mocks["run_agent"].assert_called_once()
        followup = mocks["run_agent"].call_args.args[1]
        self.assertIn("please rebase first", followup)
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))


class ValidatingHandoffSeedsAllWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The validating -> in_review handoff has to seed every comment-surface
    watermark. The orchestrator never posts inline review comments or PR
    review summaries, so `_seed_watermark_past_self` returns None for those
    surfaces; without an explicit default seed, the in_review legacy
    migration would advance past human feedback submitted on those surfaces
    during validate (the COMMENTED PR review summary case is the worst:
    `pr_has_changes_requested` does not veto the HITL ping, so the manual
    merge could land the PR over the human's note without surfacing it to
    the dev).
    """

    PR_NUMBER = 600
    BRANCH = "orchestrator/issue-200"

    def _setup(self, *, reviews=(), review_comments=()):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(200, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #600",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=list(review_comments),
            reviews=list(reviews),
        )
        gh.add_pr(pr)
        gh.seed_state(
            200, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_handoff_review_summary_surfaces_in_in_review(self) -> None:
        # A "Comment" review without `CHANGES_REQUESTED` is the dangerous
        # case: it doesn't trip `pr_has_changes_requested` so the HITL
        # ping would happily advertise the PR as ready if the in_review
        # tick advanced its watermark past the body.
        long_ago_review = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242, body="please tighten the docstring",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago_review,
            commit_id="cafe1234",
        )
        gh, issue, pr, _ = self._setup(reviews=[review])

        # Step 1: validating approves. Handoff must seed
        # pr_last_review_summary_id so the legacy in_review migration cannot
        # accidentally advance past the human review.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_summary_id", data)
        # Seeded to 0 (or any value below the review id) -- not None and not
        # past the review.
        self.assertLess(data["pr_last_review_summary_id"], 4242)

        # Step 2: in_review tick. The summary surfaces and the handler
        # routes the issue to `fixing` (the fixing handler owns the dev
        # resume cycle, not the in_review handler).
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((200, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(200).get("pending_fix_review_summary_max_id"),
            4242,
        )

    def test_pre_handoff_inline_review_comment_surfaces(self) -> None:
        # Same shape, inline-review surface. The orchestrator never posts
        # there either, so handoff has to seed pr_last_review_comment_id
        # explicitly.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr, _ = self._setup(
            review_comments=[
                FakeComment(
                    id=77, body="line 4: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_comment_id", data)
        self.assertLess(data["pr_last_review_comment_id"], 77)

        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((200, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(200).get("pending_fix_review_max_id"), 77,
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

    PR_NUMBER = 800
    BRANCH = "orchestrator/issue-300"

    def test_inline_comment_with_bot_issue_id_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(300, label="validating", comments=[
            FakeComment(
                id=4242, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=[
                # Same numeric id as the bot's issue comment above, but a
                # different namespace (PullRequestComment). The handoff must
                # not treat this as self-authored.
                FakeComment(
                    id=4242, body="please rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            300, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[4242],
            pickup_comment_id=4242,
        )

        # Step 1: validating handoff. The inline comment must NOT bump
        # pr_last_review_comment_id past 4242.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(300)
        self.assertLess(
            data.get("pr_last_review_comment_id"), 4242,
            "id collision must not advance the inline-review watermark",
        )

        # Step 2: in_review tick. The human's inline comment surfaces and
        # routes the issue to `fixing` -- no ready-for-merge ping. The
        # fixing handler owns the dev resume on the next tick.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((300, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(300).get("pending_fix_review_max_id"), 4242,
        )


class HandoffWithoutPickupIdLegacyStateTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """For an issue picked up under an older orchestrator version that did
    not record `pickup_comment_id`, the validating handoff cannot tell
    pre-pickup chatter (safe to skip) from human feedback posted during
    implementing/validating (must preserve). The seed-watermark function
    must refuse to advance past anything in that legacy state, defaulting
    pr_last_comment_id to 0; the orchestrator_comment_ids id-set filter in
    `_handle_in_review` then drops the recorded bot comments at scan time
    while leaving every human comment visible.
    """

    PR_NUMBER = 1000
    BRANCH = "orchestrator/issue-500"

    def test_legacy_human_during_implementing_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Comment id ordering models a real legacy lifecycle: pre-pickup
        # chatter, then a pickup posted by the OLD orchestrator (id 900,
        # NOT recorded in orchestrator_comment_ids), then a human "do not
        # merge yet" posted while the dev was implementing, then a
        # PR-opened comment posted by the NEW orchestrator (id 960,
        # recorded). The human comment between the two bot posts is the
        # signal that must NOT be lost.
        issue = make_issue(500, label="validating", comments=[
            FakeComment(
                id=800, body="original issue clarification",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=950, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=960, body=":sparkles: PR opened: #1000",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        # Legacy state: PR-opened (960) is the FIRST recorded bot id;
        # pickup_comment_id is missing because pickup happened under the
        # old code. Validating handoff will then see only {960} as
        # orchestrator content; the seed-watermark function must NOT
        # falsely treat ids 800/900/950 as pre-pickup chatter.
        gh.seed_state(
            500, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[960],
        )

        # Step 1: validating approves. Handoff must NOT advance the
        # watermark past 950.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        wm = gh.pinned_data(500).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must not consume legacy human feedback at id 950 "
            f"(got {wm})",
        )

        # Step 2: in_review tick. Every gate passes -- the only thing
        # standing between the PR and a ready-ping is the human's "do
        # not merge yet" comment. The handler must surface it as fresh
        # feedback and route to `fixing`.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((500, "done"), gh.label_history)
        # The "do not merge yet" comment surfaces as fresh PR feedback
        # and routes the issue to `fixing` (alongside other legacy
        # comments the migration cannot reliably classify).
        mocks["run_agent"].assert_not_called()
        self.assertIn((500, "fixing"), gh.label_history)
        # The legacy default falls through to scan from the beginning,
        # so the route bookmarks the latest visible human/issue-side id.
        self.assertGreaterEqual(
            gh.pinned_data(500).get("pending_fix_issue_max_id"), 950,
        )


class HandoffSkipsConsumedRepliesTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human reply consumed by `_resume_developer_on_human_reply` during
    implementing or validating must not re-surface as fresh PR feedback in
    in_review. The validating handoff watermark seed has to walk past such
    already-consumed comments; otherwise the next in_review tick re-routes
    the issue to `fixing` on the same human input the dev has already
    addressed.
    """

    PR_NUMBER = 1500
    BRANCH = "orchestrator/issue-900"

    def test_consumed_reply_does_not_replay_after_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> implementing dev asks question, parks
        # at 910 -> human replies "use sqlite" at 920 -> next tick resumes
        # the dev with that comment -> dev commits, _on_commits posts
        # PR-opened at 930 -> validating reviewer approves and posts
        # approval comment at 940. The reply at 920 was already fed to
        # the dev; in_review must NOT replay it.
        issue = make_issue(900, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1500",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        # `last_action_comment_id=920` reflects the post-resume bump --
        # the resume ate comments after the park (910) up through 920.
        gh.seed_state(
            900, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves. The handoff seed must walk PAST
        # comment 920 (already consumed) instead of stopping at it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(900).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertGreaterEqual(
            wm, 930,
            f"watermark must advance past consumed reply (id 920); got {wm}",
        )

        # Step 2: in_review tick. Comment 920 must NOT surface and the
        # handler reaches the manual-merge HITL ping path.
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Manual-merge-only: no merge call. The HITL ping fires because
        # the seed kept the consumed reply out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((900, "done"), gh.label_history)
        self.assertNotIn((900, "fixing"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)

    def test_resume_bumps_last_action_comment_id_to_consumed_max(self) -> None:
        # Direct unit-level check on `_resume_developer_on_human_reply`:
        # after the resume runs, `last_action_comment_id` must reflect
        # the highest consumed id, not the prior park id.

        gh = FakeGitHubClient()
        issue = make_issue(901, label="implementing", comments=[
            FakeComment(id=910, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=920, body="use sqlite", user=FakeUser("alice")),
            FakeComment(id=921, body="and add a test", user=FakeUser("alice")),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            901, dev_agent="claude", dev_session_id="dev-sess",
            last_action_comment_id=910,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", lambda *a, **kw: _agent()):
            result = workflow._resume_developer_on_human_reply(
                gh, _TEST_SPEC, issue, state
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            state.get("last_action_comment_id"), 921,
            "resume must bump last_action_comment_id to max(consumed)",
        )


class HandoffConsumedThroughIssueThreadOnlyTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`last_action_comment_id` only records issue-thread comments fed via
    `_resume_developer_on_human_reply`; PR-conversation comments are never
    consumed via that path. The validating handoff seed must NOT apply
    `consumed_through` to the PR-conversation surface, or a human PR comment
    whose id sits below a later-consumed issue-thread reply gets silently
    advanced past and the HITL ping fires over unread feedback.
    """

    PR_NUMBER = 1600
    BRANCH = "orchestrator/issue-800"

    def test_pr_conv_comment_below_consumed_through_is_preserved(self) -> None:
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
        issue = make_issue(800, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1600",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=915, body="please add a docstring to the public class",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            800, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves and seeds in_review watermarks. The
        # seed must stop before 915 so the next in_review tick scans the
        # PR-conv surface and finds the human comment. Approval routes
        # through `documenting` first (the final-docs hop).
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        self.assertIn((800, "documenting"), gh.label_history)
        wm = gh.pinned_data(800).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 915,
            "watermark must stop before unread PR-conv comment id=915 "
            f"(consumed_through=920 must NOT apply across surfaces); got {wm}",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # The PR-conv comment surfaces and the handler routes the issue
        # to `fixing` (the fixing handler owns the dev resume on the
        # next tick) instead of pinging HITL.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
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
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((800, "fixing"), gh.label_history)
        data = gh.pinned_data(800)
        self.assertGreaterEqual(data.get("pending_fix_issue_max_id"), 915)
        # The watermark stays put so the fixing handler can re-scan and
        # see id 915.
        self.assertLess(data.get("pr_last_comment_id"), 915)


class HandleValidatingResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_dev_and_stays_on_validating(self) -> None:
        # While validating (PR is open), a human edit must not discard the
        # dev's already-pushed work. Notify and resume; on a successful
        # pushed fix, stay on `validating` so the reviewer re-evaluates
        # the new diff next tick. The docs pass only runs as the
        # final-docs handoff after a fresh approval.
        gh = FakeGitHubClient()
        issue = make_issue(70, label="validating", body="updated criteria")
        gh.add_issue(issue)
        pr = FakePR(number=700, head_branch="orchestrator/issue-70")
        gh.add_pr(pr)
        gh.seed_state(
            70,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=pr.number,
            review_round=0,
            branch="orchestrator/issue-70",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="fixed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        # Stays on `validating`: no documenting hop, and the reviewer
        # has NOT been spawned this tick (the only run_agent call was
        # the dev resume).
        self.assertNotIn((70, "documenting"), gh.label_history)
        self.assertNotIn((70, "in_review"), gh.label_history)
        # Notice posted on the issue thread.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        # review_round incremented so the validating cap stays accurate.
        data = gh.pinned_data(70)
        self.assertEqual(data.get("review_round"), 1)


class ValidatingDriftDefersToReviewerRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: when validating is parked with a reviewer-side
    park reason (`reviewer_timeout` / `reviewer_failed`), a human "retry"
    comment must re-spawn the REVIEWER, not the dev session. The drift
    check fires first because the human's comment also flips the hash;
    the drift handler must defer to the awaiting-human branch in this
    case so the reviewer re-runs naturally."""

    def test_reviewer_timeout_drift_respawns_reviewer_not_dev(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1000, label="validating", body="initial body",
        )
        # Pre-existing human "retry" comment that triggers the drift
        # detection (the hash includes non-orchestrator comments).
        human = FakeComment(
            id=4000, body="retry the reviewer please",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=10000, head_branch="orchestrator/issue-1000")
        gh.add_pr(pr)
        # Pre-seed a real `user_content_hash` (the bug surfaces only
        # when the hash is already set; first-tick auto-seeding hides it).
        seed_hash = workflow._compute_user_content_hash(
            make_issue(1000, body="initial body"), set(),
        )
        gh.seed_state(
            1000,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            branch="orchestrator/issue-1000",
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=100,
            user_content_hash=seed_hash,
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-sess",
                last_message="Looks fine.\n\nVERDICT: APPROVED",
            ),
            has_new_commits=False,
            head_shas=["head"],
        )

        # The reviewer (REVIEW_AGENT) ran, NOT the dev session. The
        # agent invocation should have been against the review agent
        # binary, with a review-style prompt.
        call_args = mocks["run_agent"].call_args
        self.assertEqual(call_args[0][0], config.REVIEW_AGENT)
        self.assertIn("automated code reviewer", call_args[0][1])
        # No drift-style ":pencil2: issue body changed; resuming dev
        # session" notice was posted -- the drift was deferred.
        self.assertFalse(any(
            ":pencil2:" in body and "resuming dev session" in body
            for _, body in gh.posted_comments
        ))
        # The reviewer recovery consumed the human comment and cleared
        # the park flags.
        data = gh.pinned_data(1000)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # The new hash baseline was persisted so the next tick doesn't
        # loop on the same drift.
        new_hash = workflow._compute_user_content_hash(issue, set())
        self.assertEqual(data.get("user_content_hash"), new_hash)


class HandleValidatingVerifyGateTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Local verification gate that runs in the per-issue worktree on
    `VERDICT: APPROVED`, before the issue is labeled `in_review`. Default-
    empty `VERIFY_COMMANDS` keeps the legacy behaviour; a non-empty config
    runs each command sequentially with a bounded timeout and parks the
    issue in `validating` on any failure (non-zero exit, timeout, or a
    dirty tree left behind).
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(7, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=21,
            branch="orchestrator/issue-7",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(7, **defaults)
        return gh, issue

    def test_default_empty_verify_is_a_noop_on_approval(self) -> None:
        # With no `VERIFY_COMMANDS` configured, the gate short-circuits
        # to ok inside the runner; the helper is still called once (so a
        # future config flip toggles the gate without code changes), but
        # the approval / squash / in_review handoff path is unchanged.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("rev-sha",),
        )

        self.assertEqual(mocks["_run_verify_commands"].call_count, 1)
        # The configured commands tuple was forwarded verbatim --
        # default-empty means the runner sees ().
        call = mocks["_run_verify_commands"].call_args
        self.assertEqual(call.args[1], config.VERIFY_COMMANDS)
        self.assertEqual(config.VERIFY_COMMANDS, ())
        # Handoff completed normally through the final-docs hop.
        self.assertIn((7, "documenting"), gh.label_history)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_config_parses_semicolon_and_newline_separated_commands(self) -> None:
        # `_parse_verify_commands` accepts both `;` and `\n` separators so
        # the value fits on one line in a `.env` file. Blank lines and
        # `#`-commented lines are skipped.
        from orchestrator.config import _parse_verify_commands

        self.assertEqual(_parse_verify_commands(""), ())
        self.assertEqual(
            _parse_verify_commands("pytest -q;ruff check ."),
            ("pytest -q", "ruff check ."),
        )
        self.assertEqual(
            _parse_verify_commands("pytest -q\nruff check .\n"),
            ("pytest -q", "ruff check ."),
        )
        self.assertEqual(
            _parse_verify_commands("\n#comment\npytest -q\n\n"),
            ("pytest -q",),
        )

    def test_verify_success_keeps_existing_approval_flow(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=VerifyResult(status="ok"),
            )

        mocks["_run_verify_commands"].assert_called_once()
        # Approval comment posted; label flipped to `documenting` (the
        # final-docs hop).
        self.assertTrue(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        self.assertIn((7, "documenting"), gh.label_history)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))

    def test_verify_failed_parks_with_verify_failed_reason(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="failed",
            command="pytest -q",
            exit_code=2,
            output="E   AssertionError: bad\nTAIL_MARKER",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_failed")
        # No in_review or documenting handoff -- the verify gate fires
        # BEFORE the approval / squash / final-docs route is reached.
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        # No approval comment (gate fires BEFORE the approval post).
        self.assertFalse(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("local verification failed", last_comment)
        self.assertIn("pytest -q", last_comment)
        self.assertIn("exited with code 2", last_comment)
        self.assertIn("TAIL_MARKER", last_comment)

    def test_verify_timeout_parks_with_verify_timeout_reason(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="timeout",
            command="pytest --slow",
            exit_code=None,
            output="hanging...",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest --slow",)), \
             patch.object(config, "VERIFY_TIMEOUT", 123):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_timeout")
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("pytest --slow", last_comment)
        self.assertIn("timed out after 123s", last_comment)

    def test_verify_head_changed_parks_with_verify_head_changed_reason(self) -> None:
        # End-to-end: a verify command that moved HEAD must NOT flow
        # through to `in_review` -- otherwise squash-on-approval would
        # push the unreviewed commit. The handler parks the issue with a
        # distinct `verify_head_changed` reason so the operator can
        # adjudicate whether the auto-commit belongs in the PR.
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="head_changed",
            command="sh -c 'git commit -am autofix'",
            exit_code=0,
            output="",
            head_before="aaaa1111",
            head_after="bbbb2222",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("sh -c 'git commit -am autofix'",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_head_changed")
        # No in_review / documenting handoff and no approval / squash
        # side effects.
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        self.assertFalse(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("moved HEAD", last_comment)
        # Short SHAs are surfaced so the operator can identify the commit.
        self.assertIn("aaaa1111", last_comment)
        self.assertIn("bbbb2222", last_comment)

    def test_verify_dirty_worktree_parks(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="dirty",
            command="pytest -q",
            exit_code=0,
            dirty_files=("build/artifact.bin", "tests/cache"),
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_dirty")
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("build/artifact.bin", last_comment)

    def test_changes_requested_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        review = _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")
        # The verify mock should not be called -- assert by setting a
        # failing result that would otherwise park the issue.
        verify_fail = VerifyResult(
            status="failed", command="pytest -q", exit_code=1, output="bad",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                verify_result=verify_fail,
            )

        mocks["_run_verify_commands"].assert_not_called()
        # Standard CHANGES_REQUESTED handling: PR review comment + dev resume.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(gh.pinned_data(7).get("review_round"), 1)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))

    def test_unknown_verdict_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify_fail = VerifyResult(
            status="failed", command="pytest -q", exit_code=1, output="bad",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    last_message="I'm not sure what to think.",
                ),
                verify_result=verify_fail,
            )

        mocks["_run_verify_commands"].assert_not_called()
        data = gh.pinned_data(7)
        # Park comes from the unknown-verdict path, NOT the verify gate;
        # confirm by checking the comment text (the unknown-verdict park
        # does not persist `park_reason` to pinned state for the
        # non-silent-crash case).
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn(data.get("park_reason"), ("verify_failed", "verify_timeout", "verify_dirty"))
        self.assertIn("did not emit a VERDICT line", gh.posted_comments[-1][1])


class RunVerifyCommandsTest(unittest.TestCase):
    """Direct tests for the verify-command runner against a real shell."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Initialize a git repo so the dirty-detection branch works.
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.tmp)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.name", "t"],
            check=True,
        )
        (self.tmp / "seed").write_text("x")
        subprocess.run(
            ["git", "-C", str(self.tmp), "add", "."], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "commit", "-q", "-m", "seed"],
            check=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_commands_short_circuits_to_ok(self) -> None:
        r = workflow._run_verify_commands(self.tmp, (), 60)
        self.assertEqual(r.status, "ok")
        self.assertIsNone(r.command)

    def test_all_commands_pass_returns_ok(self) -> None:
        r = workflow._run_verify_commands(
            self.tmp, ("true", "echo hello"), 60,
        )
        self.assertEqual(r.status, "ok")

    def test_non_zero_exit_returns_failed_with_first_failing_command(self) -> None:
        r = workflow._run_verify_commands(
            self.tmp,
            ("true", "sh -c 'echo boom 1>&2; exit 3'", "true"),
            60,
        )
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.command, "sh -c 'echo boom 1>&2; exit 3'")
        self.assertEqual(r.exit_code, 3)
        self.assertIn("boom", r.output)

    def test_timeout_returns_timeout_with_partial_output(self) -> None:
        # `sleep 5` against a 1s timeout fires `TimeoutExpired`.
        r = workflow._run_verify_commands(
            self.tmp, ("sleep 5",), timeout=1,
        )
        self.assertEqual(r.status, "timeout")
        self.assertEqual(r.command, "sleep 5")
        self.assertIsNone(r.exit_code)

    def test_timeout_kills_full_process_group(self) -> None:
        # Regression: `subprocess.run(..., shell=True, timeout=...)`
        # only SIGKILLs the shell, leaving its background descendants
        # (`& subshells`, `make -j` workers, pytest-xdist forkers...)
        # alive to keep mutating the worktree after `_run_verify_commands`
        # has already returned `verify_timeout` and the orchestrator has
        # parked the issue. The runner now puts each command in its own
        # process group via `start_new_session=True` and `killpg`s the
        # group on timeout. Verified by having the verify command spawn
        # a background process that would touch a sentinel file AFTER
        # the timeout would have fired -- with the group-kill it never
        # gets to.
        marker = self.tmp / "post_timeout_marker.txt"
        # Background subshell sleeps 2s then touches the marker. Parent
        # shell sleeps 10s so the 1s timeout definitely fires. If the
        # group-kill works, the background subshell dies before its
        # sleep finishes and the marker is never created.
        cmd = (
            f"(sleep 2 && touch {marker}) & sleep 10"
        )
        r = workflow._run_verify_commands(self.tmp, (cmd,), timeout=1)
        self.assertEqual(r.status, "timeout")
        # Wait well past when the background touch would have fired.
        # 3s gives the background its full 2s + 1s of slack.
        import time
        time.sleep(3)
        self.assertFalse(
            marker.exists(),
            f"background process survived timeout-kill; {marker} was created",
        )

    def test_dirty_tree_after_success_returns_dirty(self) -> None:
        # Command exits 0 but leaves an untracked file behind.
        r = workflow._run_verify_commands(
            self.tmp, ("sh -c 'echo leak > leftover.txt'",), 60,
        )
        self.assertEqual(r.status, "dirty")
        self.assertIn("leftover.txt", r.dirty_files)

    def test_output_truncated_to_budget(self) -> None:
        big = "X" * 10000 + "TAIL"
        r = workflow._run_verify_commands(
            self.tmp,
            (f"sh -c 'printf %s {shutil_quote(big)}; exit 1'",),
            60,
        )
        self.assertEqual(r.status, "failed")
        # Tail preserved, leading bulk trimmed.
        self.assertIn("TAIL", r.output)
        self.assertLessEqual(len(r.output), 4096)

    def test_secret_straddling_truncation_boundary_is_fully_redacted(self) -> None:
        # Regression: `_redact_secrets` does `str.replace(value, "***")`
        # on the full value, so a secret whose bytes straddle the
        # truncation cut would no longer match a post-truncation replace
        # and would leak a partial value verbatim in the park comment.
        # The fix runs the redact pass BEFORE truncating so any matched
        # secret collapses to `***` before its bytes can be sliced.
        secret = "SUPERSECRET-TOKEN-VALUE-0123456789ABCDEF"  # 40 chars
        # Engineer the payload so the truncation cut (last 4096 bytes)
        # falls inside the secret rather than before it. Budget = 4096;
        # we want secret_start < (total - 4096) < secret_end so the
        # naive "truncate-then-redact" path would leak the secret's tail.
        prefix = "P" * 90
        # total = 4200 → cut at byte 104; secret occupies 90..129, so
        # bytes 14..39 of the secret (`E-0123456789ABCDEF`) would survive
        # a naive truncation.
        suffix_len = 4200 - len(prefix) - len(secret)
        suffix = "S" * suffix_len
        payload = prefix + secret + suffix
        self.assertEqual(len(payload), 4200)
        cut = len(payload) - 4096
        self.assertLess(payload.index(secret), cut)
        self.assertGreater(payload.index(secret) + len(secret), cut)

        import os as _os
        import shlex
        cmd = f"sh -c 'printf %s {shlex.quote(payload)}; exit 1'"
        with patch.dict(_os.environ, {"VERIFY_TEST_API_KEY": secret}):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        # The full secret must be gone -- baseline check.
        self.assertNotIn(secret, r.output)
        # And no 8+ char substring of the secret survives either.
        # Length 8 matches `_REDACT_MIN_VALUE_LEN`: shorter accidental
        # collisions are below the redaction threshold and tolerable.
        for start in range(len(secret) - 7):
            self.assertNotIn(
                secret[start:start + 8], r.output,
                f"partial secret substring leaked: {secret[start:start + 8]!r}",
            )
        # And the redaction marker is present (proves the runner
        # actually saw and replaced the secret).
        self.assertIn("***", r.output)

    def test_github_token_stripped_from_verify_environment(self) -> None:
        # Regression: verify commands run in the per-issue worktree
        # against code the implementer agent just produced. If the
        # runner inherited the orchestrator's process env, a prompt-
        # injected `pytest` plugin (or a hostile dependency) could read
        # `$GITHUB_TOKEN` and push or call the GitHub API as us. The
        # runner now strips via `_filter_agent_env`, mirroring what
        # `_agent_env` does for the implementer / reviewer subprocesses.
        cmd = (
            # `printenv GITHUB_TOKEN` prints the value if the var is in
            # the child env and exits 0; if unset, it prints nothing and
            # exits 1. We pipe both branches through `exit 1` so the
            # runner reports the verify as failed and we can inspect
            # `r.output` either way.
            "sh -c 'echo TOKEN_PRESENT=$([ -n \"$GITHUB_TOKEN\" ] && "
            "echo YES || echo NO); exit 1'"
        )
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK"},
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        # The verify environment must NOT carry GITHUB_TOKEN through.
        self.assertIn("TOKEN_PRESENT=NO", r.output)
        # And the original token value must not appear verbatim. (This
        # also catches a regression where redaction were doing the heavy
        # lifting instead of env stripping -- redaction would mask the
        # value with `***`, but the variable would still have been
        # exposed to the verify command.)
        self.assertNotIn("ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK", r.output)

    def test_write_credential_locators_stripped_from_verify_environment(self) -> None:
        # Issue #213 review: SSH-agent socket, askpass binaries, and
        # `GIT_SSH_COMMAND` are write-credential pointers, not secret-
        # shaped values. Leaving them in the verify shell lets a
        # hostile dependency forward through the operator's loaded
        # ssh-agent (and push to any host whose key is loaded) or
        # invoke the operator's askpass binary in their session.
        cmd = (
            "sh -c '"
            "echo SSH_AUTH=$([ -n \"$SSH_AUTH_SOCK\" ] && echo YES || echo NO); "
            "echo SSH_ASK=$([ -n \"$SSH_ASKPASS\" ] && echo YES || echo NO); "
            "echo GIT_ASK=$([ -n \"$GIT_ASKPASS\" ] && echo YES || echo NO); "
            "echo GIT_SSH=$([ -n \"$GIT_SSH_COMMAND\" ] && echo YES || echo NO); "
            "exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "SSH_AUTH_SOCK": "/tmp/ssh-test/agent.42",
                "SSH_ASKPASS": "/usr/lib/ssh/ssh-askpass",
                "GIT_ASKPASS": "/usr/share/git/askpass-helper",
                "GIT_SSH_COMMAND": "ssh -i /home/op/.ssh/deploy-key",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("SSH_AUTH=NO", r.output)
        self.assertIn("SSH_ASK=NO", r.output)
        self.assertIn("GIT_ASK=NO", r.output)
        self.assertIn("GIT_SSH=NO", r.output)
        # The locator values must not survive verbatim anywhere.
        self.assertNotIn("/tmp/ssh-test/agent.42", r.output)
        self.assertNotIn("/home/op/.ssh/deploy-key", r.output)

    def test_credential_file_locators_stripped_from_verify_environment(self) -> None:
        # Issue #213 review: credential-file LOCATORS (env vars whose
        # value is a path to a file holding the secret) must also be
        # stripped. The verify shell runs as the same OS user as the
        # orchestrator, so leaving `ORCHESTRATOR_TOKEN_FILE` /
        # `GOOGLE_APPLICATION_CREDENTIALS` / `AWS_SHARED_CREDENTIALS_FILE`
        # in the child env lets a hostile dependency simply `cat` the
        # pointer's target. The `ORCHESTRATOR_TOKEN_FILE` strip is the
        # most important case: it points at the orchestrator's own
        # write-credential file.
        cmd = (
            "sh -c '"
            "echo ORCH_TF=$([ -n \"$ORCHESTRATOR_TOKEN_FILE\" ] && "
            "echo YES || echo NO); "
            "echo GAC=$([ -n \"$GOOGLE_APPLICATION_CREDENTIALS\" ] && "
            "echo YES || echo NO); "
            "echo AWS_SCF=$([ -n \"$AWS_SHARED_CREDENTIALS_FILE\" ] && "
            "echo YES || echo NO); "
            "exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "ORCHESTRATOR_TOKEN_FILE": "/etc/secrets/orch-token-path",
                "GOOGLE_APPLICATION_CREDENTIALS": "/etc/secrets/gcp.json",
                "AWS_SHARED_CREDENTIALS_FILE": "/etc/secrets/aws-creds",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("ORCH_TF=NO", r.output)
        self.assertIn("GAC=NO", r.output)
        self.assertIn("AWS_SCF=NO", r.output)
        # And the locator path itself must not survive verbatim either
        # (env strip, not redaction-only).
        self.assertNotIn("/etc/secrets/orch-token-path", r.output)
        self.assertNotIn("/etc/secrets/gcp.json", r.output)
        self.assertNotIn("/etc/secrets/aws-creds", r.output)

    def test_production_secret_shapes_stripped_from_verify_environment(self) -> None:
        # Issue #213: GitHub-token aliases are not the only credential
        # shape that should not be inherited by operator-configured
        # verify shell. Production-secret-shaped variables (suffix or
        # bare-name matches) must be stripped too. The verify runner
        # ALSO strips the agent's provider-auth keys
        # (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) -- unlike the agent
        # subprocess case, where the allowlist preserves them, the
        # verify shell executes untrusted agent-produced code and a
        # hostile dependency reading `$ANTHROPIC_API_KEY` would gain
        # billable access to the operator's model account.
        cmd = (
            "sh -c 'echo STRIPE_PRESENT=$([ -n \"$STRIPE_API_KEY\" ] && "
            "echo YES || echo NO); "
            "echo DBPW_PRESENT=$([ -n \"$DATABASE_PASSWORD\" ] && "
            "echo YES || echo NO); "
            "echo DEPLOY_PRESENT=$([ -n \"$DEPLOY_TOKEN\" ] && "
            "echo YES || echo NO); "
            "echo ANTH_PRESENT=$([ -n \"$ANTHROPIC_API_KEY\" ] && "
            "echo YES || echo NO); "
            "echo OPENAI_PRESENT=$([ -n \"$OPENAI_API_KEY\" ] && "
            "echo YES || echo NO); exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "STRIPE_API_KEY": "sk_live_VERY_SECRET_SHOULD_NOT_LEAK",
                "DATABASE_PASSWORD": "hunter2_should_not_leak",
                "DEPLOY_TOKEN": "deploytok_should_not_leak",
                "ANTHROPIC_API_KEY": "sk-ant-SHOULD_NOT_LEAK_TO_VERIFY",
                "OPENAI_API_KEY": "sk-oai-SHOULD_NOT_LEAK_TO_VERIFY",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("STRIPE_PRESENT=NO", r.output)
        self.assertIn("DBPW_PRESENT=NO", r.output)
        self.assertIn("DEPLOY_PRESENT=NO", r.output)
        # Provider auth is stripped from the verify env -- stricter
        # than the agent-subprocess case. An operator who legitimately
        # wants to drive the agent from a verify command sets the key
        # inline (`ANTHROPIC_API_KEY=... pytest ...`).
        self.assertIn("ANTH_PRESENT=NO", r.output)
        self.assertIn("OPENAI_PRESENT=NO", r.output)
        # The stripped secret values must not appear verbatim anywhere
        # in the captured output (env strip, not redaction-only).
        self.assertNotIn("sk_live_VERY_SECRET_SHOULD_NOT_LEAK", r.output)
        self.assertNotIn("hunter2_should_not_leak", r.output)
        self.assertNotIn("deploytok_should_not_leak", r.output)
        self.assertNotIn("sk-ant-SHOULD_NOT_LEAK_TO_VERIFY", r.output)
        self.assertNotIn("sk-oai-SHOULD_NOT_LEAK_TO_VERIFY", r.output)

    def test_command_that_commits_is_caught_as_head_changed(self) -> None:
        # Regression: a verify command that runs `git commit` leaves
        # `git status --porcelain` clean and exits 0, so the previous
        # dirty+exit-code-only gate accepted it as "ok". The squash-on-
        # approval + force-push that followed would then publish the
        # unreviewed verify-created commit to the PR branch. Snapshotting
        # HEAD before the loop and refusing any command that moves it
        # closes that hole.
        head_before = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Stage and commit a new file inside the verify command itself --
        # exactly the dangerous shape (a verify rule that auto-fixes and
        # commits its own fix).
        cmd = (
            "sh -c 'echo VERIFY_AUTO_FIXED > autofix.txt && "
            "git add autofix.txt && "
            "git commit -q -m \"chore: verify-time auto-fix\"'"
        )
        r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "head_changed")
        self.assertEqual(r.command, cmd)
        self.assertEqual(r.head_before, head_before)
        self.assertNotEqual(r.head_after, head_before)
        # And the worktree was clean on detection (not the dirty branch).
        self.assertEqual(r.dirty_files, ())

    def test_dirty_attribution_names_responsible_command_and_keeps_output(self) -> None:
        # Regression: previously the dirty check ran once at the end of
        # the loop, so a dirty failure always blamed `commands[-1]` and
        # discarded every command's captured output. The fix checks
        # dirtiness AFTER EACH command so the actual command that left
        # the worktree dirty is named, with its own stdout/stderr
        # preserved for the park comment.
        cmds = (
            "true",                                              # clean, exit 0
            "sh -c 'echo BUILD_LOG_LINE; touch leftover.txt'",   # leaves untracked file
            "true",                                              # should never run
        )
        r = workflow._run_verify_commands(self.tmp, cmds, 60)
        self.assertEqual(r.status, "dirty")
        # Named command is the SECOND command (the one that left the
        # tree dirty), NOT `commands[-1]`.
        self.assertEqual(r.command, cmds[1])
        self.assertEqual(r.exit_code, 0)
        # The dirty file lands in `dirty_files`.
        self.assertIn("leftover.txt", r.dirty_files)
        # The command's stdout is preserved for the park comment so the
        # operator can triage what the command actually did.
        self.assertIn("BUILD_LOG_LINE", r.output)


def shutil_quote(s: str) -> str:
    """Local shell-quote helper for the truncate test -- avoids importing
    `shlex` at module scope when it is only used by one test."""
    import shlex
    return shlex.quote(s)


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
            head_branch="orchestrator/issue-120",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            120, pr_number=12000, branch="orchestrator/issue-120",
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
            head_branch="orchestrator/issue-121",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            121, pr_number=12100, branch="orchestrator/issue-121",
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
    BRANCH = "orchestrator/issue-9"
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
