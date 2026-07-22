# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    EVENT_AGENT_SPAWN,
    LABEL_DOCUMENTING,
    LABEL_FIXING,
    LABEL_IN_REVIEW,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    REVIEW_CHANGES_REQUESTED_MESSAGE,
    ROLE_DEVELOPER,
    ROLE_REVIEWER,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

FRESH_REVIEW_ISSUE = 5
FRESH_REVIEW_PR = 11
FIX_LOOP_ISSUE = 6
FIX_LOOP_PR = 12
HUMAN_RESUME_ISSUE = 7
RESUME_PR = 13
SILENT_STREAK_ISSUE = 70
SILENT_STREAK_PR = 14
REVIEW_CAP_ISSUE = 80
REVIEW_CAP_PR = 15
CAP_MESSAGE_ISSUE = 81
CAP_MESSAGE_PR = 16
CAP_REASON_ISSUE = 82
CAP_REASON_PR = 17
CAP_RECOVERY_ISSUE = 83
SECONDARY_PR = 18
INTERRUPTED_RESUME_PR = 19
TRUST_CAP_ISSUE = 90
TRUST_RETRY_ISSUE = 91
HUMAN_COMMENT_ID = 1100
LATEST_COMMAND_ID = 1101
FOLLOWUP_COMMENT_ID = 1200
HUMAN_RETRY_COMMENT_ID = 1300
ACTION_COMMENT_ID = 950
PICKUP_COMMENT_ID = 900
CAP_COMMAND_ID = 2000
STDERR_PAYLOAD_SIZE = 8192
STDERR_PREFIX_SIZE = 4096
DEV_SESSION = "dev-sess"
RUN_AGENT = "run_agent"
FIXED_MESSAGE = "fixed"
BEFORE_FIX_SHA = "aaa"
AFTER_FIX_SHA = "bbb"
PRE_FIX_SHA = "sha-before"
PUSH_BRANCH = "_push_branch"
REVIEW_ROUND = "review_round"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
AGENT_ROLE = "agent_role"
EVENT_NAME = "event"
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
HUMAN_LOGIN = "alice"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"
REVIEW_CAP = "review_cap"
ADD_ONE_ROUND_COMMAND = "/orchestrator add-review-rounds 1"
CAP_RESET_MESSAGE = "review-cap reset"
USER_CONTENT_HASH = "user_content_hash"
ALLOWED_AUTHORS_SETTING = "ALLOWED_ISSUE_AUTHORS"
ALLOWED_AUTHORS = ("geserdugarov",)


class _FreshReviewFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_REVIEW_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        defaults = dict(
            pr_number=FRESH_REVIEW_PR,
            branch=_issue_branch(FRESH_REVIEW_ISSUE),
            codex_session_id=DEV_SESSION,
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(FRESH_REVIEW_ISSUE, **defaults)
        return gh, issue


class HandleValidatingFreshReviewTest(
    unittest.TestCase,
    _FreshReviewFixtureMixin,
):
    """Route explicit review verdicts and surface unknown output."""

    def test_approved_flips_label_and_does_not_resume(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
        )

        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        # Approval routes through `documenting` for the final docs pass
        # before in_review picks up.
        self.assertIn((5, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((5, LABEL_IN_REVIEW), gh.label_history)
        self.assertTrue(
            any(
                ":white_check_mark: codex review approved" in body
                for _, body in gh.posted_pr_comments
            )
        )

    def test_changes_requested_resume_and_bump_round(self) -> None:
        gh, issue = self._seeded()
        review = _agent(
            session_id="rev-sess",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        dev_fix = _agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE)

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            # 1: before_sha for the dev-fix run. 2: after_sha to confirm
            # the new commit.
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        self.assertEqual(mocks[RUN_AGENT].call_count, 2)
        # Second call (dev fix) must resume the developer session.
        _, second_kwargs = mocks[RUN_AGENT].call_args_list[1]
        self.assertEqual(second_kwargs.get("resume_session_id"), DEV_SESSION)

        self.assertTrue(
            any(
                ":eyes: codex review (round 1/" in body
                and "Fix typo" in body
                for _, body in gh.posted_pr_comments
            )
        )
        mocks[PUSH_BRANCH].assert_called_once()
        self.assertEqual(gh.pinned_data(5).get(REVIEW_ROUND), 1)
        # The dev-fix subphase runs under the `fixing` label so the active
        # job is observably "fixing reviewer-requested changes" rather
        # than "validating". On a successful pushed fix the handler flips
        # back to `validating` so the reviewer re-evaluates the new head
        # on the next tick. No documenting hop -- the docs pass only runs
        # as the final-docs handoff after approval.
        self.assertIn((5, LABEL_FIXING), gh.label_history)
        # The trailing label entry must be `validating` so the next tick
        # picks up via `_handle_validating`.
        self.assertEqual(gh.label_history[-1], (5, LABEL_VALIDATING))
        # The `fixing` flip happens BEFORE the `validating` flip so an
        # external observer sees the active work labeled `fixing` for the
        # duration of the dev subprocess.
        fixing_idx = gh.label_history.index((5, LABEL_FIXING))
        validating_idx = gh.label_history.index((5, LABEL_VALIDATING))
        self.assertLess(fixing_idx, validating_idx)
        self.assertNotIn((5, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((5, LABEL_IN_REVIEW), gh.label_history)

    def test_unknown_verdict_parks_with_message(self) -> None:
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                last_message="I'm not sure what to think",
                stderr="some subprocess noise",
            ),
        )

        self.assertTrue(gh.pinned_data(5).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertIn("> I'm not sure what to think", last_comment)
        # Real reviewer text is present, so the operator does not need
        # subprocess stderr in addition -- skip the diagnostic block.
        self.assertNotIn("Reviewer stderr", last_comment)
        # Label stays validating: no in_review transition.
        self.assertNotIn((5, LABEL_IN_REVIEW), gh.label_history)

    def test_empty_review_park_shows_stderr_exit(self) -> None:
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
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(
                    last_message="",
                    stderr=cf_blob,
                    exit_code=2,
                ),
            )

        self.assertTrue(gh.pinned_data(5).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertIn("(reviewer produced no final message)", last_comment)
        self.assertIn("_Reviewer stderr (last 1KB):_", last_comment)
        self.assertIn("Enable JavaScript and cookies", last_comment)
        self.assertIn("_Reviewer exit code:_ 2", last_comment)
        # Same data flowed to a WARNING log so operators tailing the
        # orchestrator log don't have to read GitHub to triage.
        self.assertTrue(
            any(
                "reviewer emitted no VERDICT" in record.getMessage() and "exit_code=2" in record.getMessage()
                for record in logs.records
            )
        )

    def test_empty_review_park_truncates_long_stderr(self) -> None:
        # A multi-MB CF response must not bloat the issue body. The
        # park comment caps stderr at 1KB.
        gh, issue = self._seeded()
        padding = "X" * STDERR_PAYLOAD_SIZE
        huge = f"{padding}TAIL_MARKER"
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message="", stderr=huge, exit_code=1),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("TAIL_MARKER", last_comment)
        # The leading head of the noise must be dropped by the cap.
        self.assertNotIn("X" * STDERR_PREFIX_SIZE, last_comment)

    def test_empty_review_without_stderr_omits_block(self) -> None:
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message="", stderr=""),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertNotIn("_Reviewer stderr", last_comment)
        self.assertNotIn("_Reviewer exit code:_", last_comment)


class HandleValidatingReviewerFailureTest(
    unittest.TestCase,
    _FreshReviewFixtureMixin,
):
    """Classify timeout, crash, and silent reviewer outcomes."""

    def test_reviewer_timeout_parks(self) -> None:
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(timed_out=True),
        )

        state = gh.pinned_data(5)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # Tagged transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        self.assertEqual(state.get(PARK_REASON), "reviewer_timeout")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("reviewer timed out", last_comment)
        self.assertNotIn((5, LABEL_IN_REVIEW), gh.label_history)

    def test_silent_crash_parks_reviewer_failed(self) -> None:
        # The reviewer agent crashed (e.g. codex returned `Error: No such
        # file or directory (os error 2)`): empty last_message + non-zero
        # exit code. Tag the park as `reviewer_failed` so the next tick's
        # transient-recovery branch re-spawns the reviewer silently
        # without needing a human comment.
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message="", stderr="boom", exit_code=2),
        )

        state = gh.pinned_data(5)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), "reviewer_failed")

    def test_text_unknown_verdict_not_tagged_failed(self) -> None:
        # When the reviewer DID emit text but no VERDICT line, the park
        # is real adjudication and must NOT be silently retried -- a
        # human needs to read the message. Park reason stays cleared.
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                last_message="not sure what to think",
                exit_code=0,
            ),
        )

        state = gh.pinned_data(5)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))

    def test_empty_zero_exit_message_not_failed(self) -> None:
        # Defensive: empty last_message but exit_code == 0 is not a
        # crash -- the agent reported success without producing output.
        # Don't tag transient; a clean exit with no text needs human
        # adjudication, not a silent retry that would loop the same way.
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message="", stderr="", exit_code=0),
        )

        state = gh.pinned_data(5)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))


class _FixLoopFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, *, stale_label_cache=False, **state):
        gh = FakeGitHubClient(stale_label_cache=stale_label_cache)
        issue = make_issue(6, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        defaults = dict(
            pr_number=FIX_LOOP_PR,
            branch=_issue_branch(6),
            codex_session_id=DEV_SESSION,
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(6, **defaults)
        return gh, issue

    def _changes_requested_review(self):
        return _agent(
            session_id="rev-sess",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )


class HandleValidatingFixLoopEdgeCasesTest(
    unittest.TestCase,
    _FixLoopFixtureMixin,
):
    """Keep unsafe reviewer-requested fixes parked without round drift."""

    def test_dev_fix_timeout_parks_agent_timeout(self) -> None:
        # The dev agent timed out mid-fix. The park must be tagged so the
        # next tick's recovery branch can rerun the reviewer instead of
        # waiting for a human comment that the timeout itself cannot
        # produce. The pre-agent SHA must also be persisted so recovery
        # can tell whether the agent committed before timing out (the
        # naive `_has_new_commits()` check is unconditionally true for a
        # PR worktree past its first fix).
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, timed_out=True),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA],
        )

        state = gh.pinned_data(6)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), "agent_timeout")
        # `head_shas` are consumed in order: before_sha is "aaa", which
        # is what gets persisted.
        self.assertEqual(state.get("pre_dev_fix_sha"), BEFORE_FIX_SHA)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent timed out", last_comment)
        # CHANGES_REQUESTED flips the label to `fixing` BEFORE the dev
        # spawn so a parked subprocess leaves the active job labeled
        # `fixing` (the fixing handler then owns the awaiting-human
        # rescan + dev resume cycle on subsequent ticks).
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertNotIn((6, LABEL_VALIDATING), gh.label_history)

    def test_no_commit_fix_parks_without_round_bump(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, last_message="why?"),
            ],
            dirty_files=(),
            push_branch=True,
            # before_sha + after_sha (both "aaa" -> no commit).
            head_shas=[BEFORE_FIX_SHA, BEFORE_FIX_SHA],
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get(REVIEW_ROUND), 0)
        self.assertTrue(gh.pinned_data(6).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent needs your input", last_comment)
        # The pre-spawn label flip is observed even on the no-commit park
        # path (the fixing handler then handles the awaiting-human rescan
        # on the next tick).
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertNotIn((6, LABEL_VALIDATING), gh.label_history)

    def test_dev_fix_dirty_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, last_message="partial"),
            ],
            dirty_files=["leftover.py"],
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get(REVIEW_ROUND), 0)
        self.assertTrue(gh.pinned_data(6).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("leftover.py", last_comment)
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertNotIn((6, LABEL_VALIDATING), gh.label_history)

    def test_dev_fix_push_fail_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            ],
            dirty_files=(),
            push_branch=False,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        state = gh.pinned_data(6)
        self.assertEqual(state.get(REVIEW_ROUND), 0)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # The transient `push_failed` tag is what lets the next tick's
        # recovery branch silently retry the push without needing a human
        # comment to unstick the issue.
        self.assertEqual(state.get(PARK_REASON), "push_failed")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertNotIn((6, LABEL_VALIDATING), gh.label_history)

    def test_round_cap_parks_without_reviewer(self) -> None:
        gh, issue = self._seeded(review_round=config.MAX_REVIEW_ROUNDS)
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertTrue(gh.pinned_data(6).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("review still has comments", last_comment)


class HandleValidatingFixLoopRoutingTest(
    unittest.TestCase,
    _FixLoopFixtureMixin,
):
    """Expose the fixing subphase and return pushed fixes to validating."""

    def test_enters_fixing_before_dev_spawn(self) -> None:
        # The dev-fix subphase must run under the `fixing` label so the
        # active job is observably "fixing reviewer-requested changes"
        # rather than "validating". The label flip lands BEFORE the dev
        # subprocess so an external observer never sees the dev work
        # labeled only `validating`; the `fixing` entry must therefore
        # appear in the label history strictly before any later flip
        # back to `validating`.
        #
        # `stale_label_cache` reproduces PyGithub: `set_labels(FIXING)`
        # writes the remote but leaves the cached `issue.labels` at
        # `validating`, so the dev-run stage cannot be read back off the
        # issue -- the reviewer-requested fix path must pass it explicitly.
        gh, issue = self._seeded(stale_label_cache=True)
        self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        # Both flips landed in order: first `fixing` (pre-spawn), then
        # `validating` (post-push) so the reviewer reruns on the next tick.
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertIn((6, LABEL_VALIDATING), gh.label_history)
        fixing_idx = gh.label_history.index((6, LABEL_FIXING))
        validating_idx = gh.label_history.index((6, LABEL_VALIDATING))
        self.assertLess(fixing_idx, validating_idx)
        # Reviewer work stays attributed to `validating`; the CHANGES_REQUESTED
        # developer fix is attributed to `fixing` even though the resume runs
        # on the same `Issue` object whose cached labels still read
        # `validating`. Attributing the fix to `validating` would double-count
        # its spend against the reviewer/verify bucket.
        spawns_by_role = {
            event[AGENT_ROLE]: event for event in gh.recorded_events if event[EVENT_NAME] == EVENT_AGENT_SPAWN
        }
        self.assertEqual(spawns_by_role[ROLE_REVIEWER]["stage"], LABEL_VALIDATING)
        self.assertEqual(spawns_by_role[ROLE_DEVELOPER]["stage"], LABEL_FIXING)

    def test_interrupted_fix_skips_write_and_push(self) -> None:
        # A shutdown-killed CHANGES_REQUESTED dev resume is ignored: the
        # handler does NOT persist the post-spawn state (so the per-session
        # resume budget `dev_resume_count` charged by `_resume_dev_with_text`
        # is not burned) and does NOT push. The pre-spawn `fixing` flip
        # stands; the next tick re-runs the cycle. Any local commit the
        # killed run left is republished by `_handle_dev_fix_result`'s
        # stranded-fix gate on the next clean resume, not this interrupted
        # one.
        gh, issue = self._seeded(dev_agent=BACKEND_CLAUDE, dev_session_id=DEV_SESSION)
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(
                    session_id=DEV_SESSION,
                    interrupted=True,
                    last_message="committed a partial fix before the SIGTERM",
                ),
            ],
            head_shas=[BEFORE_FIX_SHA],
        )

        # Reviewer + dev resume both ran.
        self.assertEqual(mocks[RUN_AGENT].call_count, 2)
        # The interrupted run is not pushed.
        mocks[PUSH_BRANCH].assert_not_called()
        # Pre-spawn flip landed; the issue did NOT bounce to validating this
        # tick (that happens on a later tick after a clean re-review).
        self.assertIn((6, LABEL_FIXING), gh.label_history)
        self.assertNotIn((6, LABEL_VALIDATING), gh.label_history)
        state = gh.pinned_data(6)
        # Post-spawn write skipped: the resume-budget charge from
        # `_resume_dev_with_text` never persisted.
        self.assertIsNone(state.get("dev_resume_count"))
        # Interrupted is not a question / timeout / dirty park.
        self.assertFalse(state.get(AWAITING_HUMAN))

    def test_change_park_records_reviewer_anchor(self) -> None:
        # #742: on the validating -> fixing route the reviewer-feedback PR
        # comment id is anchored in `pending_fix_reviewer_comment_id` so a
        # session-failure park is retryable by `/orchestrator continue`.
        # `pending_fix_at` must stay UNSET -- it is the in_review-route
        # discriminator that drives the review-round reset, so setting it here
        # would mis-account the round on the eventual pushed fix.
        gh, issue = self._seeded()
        self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                # No-commit park: the dev asks a question / goes silent.
                _agent(session_id=DEV_SESSION, last_message="why?"),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, BEFORE_FIX_SHA],
        )

        state = gh.pinned_data(6)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # The reviewer feedback is anchored, and its id matches the PR comment
        # the handler posted this tick.
        self.assertIsNotNone(state.get("pending_fix_reviewer_comment_id"))
        # The in_review-route discriminator is NOT set on this route.
        self.assertIsNone(state.get("pending_fix_at"))
        self.assertIn((6, LABEL_FIXING), gh.label_history)

    def test_pushed_fix_clears_reviewer_anchor(self) -> None:
        # On a pushed inline fix this reviewer round is addressed, so the
        # anchor is cleared (a later session-failure park must not replay it)
        # and the round bumps back on `validating`.
        gh, issue = self._seeded(review_round=2)
        self._run_validating(
            gh,
            issue,
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        state = gh.pinned_data(6)
        self.assertIsNone(state.get("pending_fix_reviewer_comment_id"))
        self.assertEqual(state.get(REVIEW_ROUND), 3)
        self.assertEqual(gh.label_history[-1], (6, LABEL_VALIDATING))


class HandleValidatingAwaitingHumanResumeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_human_reply_bumps_round_without_reviewer(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body="use sqlite please",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            7,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            codex_session_id=DEV_SESSION,
            review_round=1,
            pr_number=RESUME_PR,
            branch=_issue_branch(7),
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        # Only the dev resume runs this tick; the reviewer fires on the next.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get("resume_session_id"), DEV_SESSION)
        followup = call.args[1]
        self.assertIn("use sqlite please", followup)

        mocks[PUSH_BRANCH].assert_called_once()
        state = gh.pinned_data(7)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        # A successful awaiting-human resume stays on `validating` (no
        # documenting hop) so the reviewer re-runs against the new head
        # on the next tick.
        self.assertNotIn((7, LABEL_DOCUMENTING), gh.label_history)
        self.assertNotIn((7, LABEL_IN_REVIEW), gh.label_history)

    def test_successful_fix_resets_silent_streak(self) -> None:
        # The validating / in_review fix paths exit on `_handle_dev_fix_result`
        # returning True without going through `_on_commits`. Without an
        # explicit reset on that branch, `silent_park_count` would still
        # carry over from earlier silent parks, and a later single empty
        # resume could tip an otherwise-healthy session past the
        # fresh-session threshold.
        gh = FakeGitHubClient()
        issue = make_issue(SILENT_STREAK_ISSUE, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body="please fix it",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            SILENT_STREAK_ISSUE,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_number=SILENT_STREAK_PR,
            branch=_issue_branch(SILENT_STREAK_ISSUE),
            # Carryover from an earlier silent park; one short of the
            # fresh-session threshold.
            silent_park_count=1,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        state = gh.pinned_data(SILENT_STREAK_ISSUE)
        self.assertEqual(
            state.get("silent_park_count"),
            0,
            "a successful dev fix must reset the silent-park streak so a "
            "later transient empty result doesn't drop a healthy session",
        )


class _ContinueCommandFixtureMixin(_PatchedWorkflowMixin):
    def _seed(self, number, *, park_reason, command="/orchestrator continue"):
        gh = FakeGitHubClient()
        issue = make_issue(number, label=LABEL_VALIDATING, body="the requirements")
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body=command,
                user=FakeUser("dave"),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            number,
            awaiting_human=True,
            park_reason=park_reason,
            last_action_comment_id=ACTION_COMMENT_ID,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            silent_park_count=1,
            review_round=1,
            pr_number=RESUME_PR,
            branch=_issue_branch(number),
            # Current content hash so a bare continue (excluded from the hash)
            # does not fire drift: the continue gate, not drift, is under test.
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        return gh, issue


class HandleValidatingContinueCommandTest(
    unittest.TestCase,
    _ContinueCommandFixtureMixin,
):
    """Retry transient parks without forwarding the continue command."""

    def test_bare_continue_retries_without_literal(
        self,
    ) -> None:
        gh, issue = self._seed(7, park_reason="agent_silent")

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            dirty_files=(),
            push_branch=True,
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
        )

        # The dev is resumed on the neutral retry prompt, NOT the literal command.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        followup = mocks[RUN_AGENT].call_args.args[1]
        self.assertIn("session/usage limit", followup)
        self.assertNotIn("/orchestrator continue", followup)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs.get("resume_session_id"),
            DEV_SESSION,
        )
        # No spurious drift notice; the fix pushed and the round bumped.
        self.assertFalse(
            any(
                "issue body changed" in body
                for _, body in gh.posted_comments
            )
        )
        state = gh.pinned_data(7)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)

    def test_bare_continue_on_question_park_refuses(self) -> None:
        gh, issue = self._seed(8, park_reason=None)

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertTrue(
            any(
                "needs your actual guidance" in body
                for _, body in gh.posted_comments
            )
        )
        state = gh.pinned_data(8)
        self.assertTrue(state.get(AWAITING_HUMAN))


class _ReviewCapFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, *, comment_body: Optional[str] = None, **state):
        gh = FakeGitHubClient()
        issue = make_issue(REVIEW_CAP_ISSUE, label=LABEL_VALIDATING)
        if comment_body is not None:
            issue.comments.append(
                FakeComment(
                    id=HUMAN_COMMENT_ID,
                    body=comment_body,
                    user=FakeUser(HUMAN_LOGIN),
                )
            )
        gh.add_issue(issue)
        defaults = dict(
            awaiting_human=True,
            park_reason=REVIEW_CAP,
            last_action_comment_id=ACTION_COMMENT_ID,
            review_round=config.MAX_REVIEW_ROUNDS,
            dev_session_id=DEV_SESSION,
            dev_agent=BACKEND_CODEX,
            pr_number=REVIEW_CAP_PR,
            branch=_issue_branch(REVIEW_CAP_ISSUE),
        )
        defaults.update(state)
        gh.seed_state(REVIEW_CAP_ISSUE, **defaults)
        return gh, issue


class HandleValidatingReviewCapAddRoundsCommandTest(
    unittest.TestCase,
    _ReviewCapFixtureMixin,
):
    """Reset a review-cap park with a valid operator command."""

    def test_command_resets_round_park_and_reruns(self) -> None:
        # Granting 1 more round on a 3-cap means review_round becomes 2.
        # The reviewer-spawn block fires on the SAME tick (fall-through
        # parity with the reviewer_timeout / reviewer_failed branches) so
        # the operator does not have to wait an extra poll for the
        # reviewer to actually rerun.
        gh, issue = self._seeded(
            comment_body=ADD_ONE_ROUND_COMMAND,
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
            head_shas=[BEFORE_FIX_SHA],
        )

        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        # Watermark advanced past the operator's command comment so the
        # next tick doesn't re-fire the same command.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        # Reviewer ran THIS tick (parity with reviewer_timeout fall-through).
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        reviewer_spawns = [
            event
            for event in gh.recorded_events
            if event[EVENT_NAME] == EVENT_AGENT_SPAWN and event.get(AGENT_ROLE) == ROLE_REVIEWER
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(
            reviewer_spawns[0][REVIEW_ROUND],
            config.MAX_REVIEW_ROUNDS - 1,
        )
        # Confirmation comment posted on the issue.
        self.assertTrue(
            any(
                CAP_RESET_MESSAGE in body
                and "granting 1 more round" in body
                for _, body in gh.posted_comments
            )
        )

    def test_max_n_grants_full_reset(
        self,
    ) -> None:
        # `N >= MAX_REVIEW_ROUNDS` clamps review_round to 0 -- the full
        # reset. The reviewer-spawn block then runs with a fresh budget.
        requested_rounds = config.MAX_REVIEW_ROUNDS + 5
        gh, issue = self._seeded(
            comment_body=f"/orchestrator add-review-rounds {requested_rounds}",
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )

        self.assertEqual(gh.pinned_data(REVIEW_CAP_ISSUE).get(REVIEW_ROUND), 0)

    def test_command_picks_latest(self) -> None:
        # Two commands in the same batch: the later one wins so a
        # corrected post supersedes a stale typo without needing the
        # operator to delete the first comment.
        gh, issue = self._seeded()
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body=ADD_ONE_ROUND_COMMAND,
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        issue.comments.append(
            FakeComment(
                id=LATEST_COMMAND_ID,
                body="actually scratch that\n/orchestrator add-review-rounds 2",
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )

        self.assertEqual(
            gh.pinned_data(REVIEW_CAP_ISSUE).get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 2,
        )
        self.assertEqual(gh.pinned_data(REVIEW_CAP_ISSUE).get(LAST_ACTION_COMMENT_ID), LATEST_COMMAND_ID)

    def test_zero_command_rejected_and_parked(self) -> None:
        gh, issue = self._seeded(
            comment_body="/orchestrator add-review-rounds 0",
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No agent ran: the error path stays parked, doesn't fall through.
        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        # Round is unchanged.
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS,
        )
        # Watermark advanced so the operator can post a corrected command
        # in a new comment without re-tripping the same rejection.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("ignored", last_comment)
        self.assertIn("positive integer", last_comment)

    def test_plain_reply_stays_parked(self) -> None:
        # The original bug: on a `review_cap` park, a plain human reply
        # used to wake the dev session and the reviewer rebumped past
        # the cap on the next tick. The new behavior is to stay parked
        # silently when no command is present; only the explicit command
        # can restart the loop.
        gh, issue = self._seeded(comment_body="any luck on this?")

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        # Watermark NOT advanced -- the operator may still post the
        # command later in a follow-up comment, and we need to see it.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), ACTION_COMMENT_ID)


class HandleValidatingReviewCapCommandGuardTest(
    unittest.TestCase,
    _ReviewCapFixtureMixin,
):
    """Reject misplaced commands and advertise real cap recovery."""

    def test_command_only_fires_on_review_cap_park(self) -> None:
        # A command posted under a different park reason (here: a
        # standard dev-question park with `park_reason=None`) must NOT
        # take the cap-reset branch. The dev resume runs as usual.
        gh, issue = self._seeded(
            comment_body=ADD_ONE_ROUND_COMMAND,
            park_reason=None,
            review_round=1,
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
            dirty_files=(),
            push_branch=True,
        )

        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        # Dev resume bumped the round; no cap-reset semantics applied.
        self.assertEqual(state.get(REVIEW_ROUND), 2)
        # No reset confirmation comment was posted.
        self.assertFalse(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in gh.posted_comments
            )
        )

    def test_command_inline_in_prose_does_not_fire(self) -> None:
        # The regex requires the command at the start of a line, so a
        # quote of the syntax in regular prose (e.g. the operator asking
        # someone else how to use it) does not trigger the reset.
        gh, issue = self._seeded(
            comment_body=("do we just run `/orchestrator add-review-rounds 1` here?"),
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS,
        )

    def test_cap_park_advertises_command(self) -> None:
        # When the orchestrator first parks on the cap, the park comment
        # itself surfaces the command so an operator who has never seen
        # the syntax can copy/paste it from the issue thread.
        gh = FakeGitHubClient()
        issue = make_issue(CAP_MESSAGE_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            CAP_MESSAGE_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=CAP_MESSAGE_PR,
            branch=_issue_branch(CAP_MESSAGE_ISSUE),
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("/orchestrator add-review-rounds", last_comment)

    def test_cap_park_persists_reason(self) -> None:
        # `_park_awaiting_human` always clears `park_reason` to None (its
        # `reason=` kwarg only feeds the audit event), so the cap branch
        # must re-set the durable field itself. Without this, the next
        # tick's awaiting-human dispatch sees `park_reason=None` and the
        # `/orchestrator add-review-rounds` parser never runs -- the
        # command would silently fall through to the dev-resume branch.
        gh = FakeGitHubClient()
        issue = make_issue(CAP_REASON_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            CAP_REASON_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=CAP_REASON_PR,
            branch=_issue_branch(CAP_REASON_ISSUE),
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        state = gh.pinned_data(CAP_REASON_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)

    def test_command_fires_after_real_cap_park(self) -> None:
        # End-to-end regression for the original bug: the FIRST tick must
        # park via the cap branch (not pre-seeded shortcut), persist
        # `park_reason="review_cap"`, and seed a `user_content_hash`. The
        # SECOND tick must then bypass the user-content-drift branch
        # (the operator's command comment changes the hash by definition)
        # and route through the cap-reset path so the round actually
        # resets. Pre-seeded tests above cover the command parser in
        # isolation; this one closes the loop on the production sequence.
        gh = FakeGitHubClient()
        issue = make_issue(CAP_RECOVERY_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            CAP_RECOVERY_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=SECONDARY_PR,
            branch=_issue_branch(CAP_RECOVERY_ISSUE),
            pickup_comment_id=PICKUP_COMMENT_ID,
            dev_session_id=DEV_SESSION,
            dev_agent=BACKEND_CODEX,
        )

        # Tick 1: cap park.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )
        tick1 = gh.pinned_data(CAP_RECOVERY_ISSUE)
        self.assertTrue(tick1.get(AWAITING_HUMAN))
        self.assertEqual(tick1.get(PARK_REASON), REVIEW_CAP)
        # The user-content baseline got seeded on the cap tick (either
        # by the drift helper's first-call branch or via the orchestrator's
        # own park comment routing). Either way the next tick has a hash
        # to compare against.
        self.assertIsInstance(tick1.get(USER_CONTENT_HASH), str)
        baseline_hash = tick1[USER_CONTENT_HASH]

        # Operator posts the command after the cap park. This is a
        # non-orchestrator comment, so it shifts the content hash --
        # without the drift-block bypass the next tick would resume the
        # dev session on a body-edit prompt and never see the command.
        issue.comments.append(
            FakeComment(
                id=CAP_COMMAND_ID,
                body=ADD_ONE_ROUND_COMMAND,
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        # Tick 2: command processes through the cap-reset path.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )
        tick2 = gh.pinned_data(CAP_RECOVERY_ISSUE)
        self.assertFalse(tick2.get(AWAITING_HUMAN))
        self.assertIsNone(tick2.get(PARK_REASON))
        self.assertEqual(
            tick2.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        self.assertEqual(tick2.get(LAST_ACTION_COMMENT_ID), CAP_COMMAND_ID)
        # The drift block updates the baseline as it falls through, so
        # the new hash should be persisted -- but the resumed-dev-session
        # drift message must NOT have been posted.
        self.assertNotEqual(tick2.get(USER_CONTENT_HASH), baseline_hash)
        self.assertFalse(
            any(
                "issue body changed; resuming dev session" in body
                for _, body in gh.posted_comments
            )
        )
        # The cap-reset confirmation landed AND the reviewer ran with
        # the freshly-reset round.
        self.assertTrue(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in gh.posted_comments
            )
        )
        reviewer_spawns = [
            event
            for event in gh.recorded_events
            if (
                event[EVENT_NAME] == EVENT_AGENT_SPAWN
                and event.get(AGENT_ROLE) == ROLE_REVIEWER
            )
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(
            reviewer_spawns[0][REVIEW_ROUND],
            config.MAX_REVIEW_ROUNDS - 1,
        )


class _InterruptedFixFixtureMixin:
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(7, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(7, **state)
        return gh, gh.read_pinned_state(issue), issue


class ValidatingDevFixInterruptedHelperTest(
    unittest.TestCase,
    _InterruptedFixFixtureMixin,
):
    """Ignore shutdown-interrupted results before fix disposition."""

    def test_false_without_side_effects(
        self,
    ) -> None:
        gh, state, issue = self._seeded()
        agent_result = _agent(
            session_id=DEV_SESSION,
            interrupted=True,
            last_message="partial output before the shutdown SIGTERM",
        )

        pushed = workflow._handle_dev_fix_result(
            gh,
            _TEST_SPEC,
            issue,
            state,
            Path("/tmp/wt"),
            agent_result,
            PRE_FIX_SHA,
        )

        self.assertFalse(pushed)
        # No park: awaiting_human untouched, no transient reason tagged, no
        # timeout watermark persisted.
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertIsNone(state.get("pre_dev_fix_sha"))
        # No HITL / question comment posted on either surface.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_content_change_returns_parked(
        self,
    ) -> None:
        gh, state, issue = self._seeded()
        agent_result = _agent(
            session_id=DEV_SESSION,
            interrupted=True,
            last_message="ACK: looks fine",  # partial; must NOT be honored
        )

        outcome = workflow._post_user_content_change_result(
            gh,
            _TEST_SPEC,
            issue,
            state,
            Path("/tmp/wt"),
            agent_result,
            PRE_FIX_SHA,
        )

        # Reported parked, but WITHOUT swallowing the partial message as an
        # ack or parking awaiting_human.
        self.assertEqual(outcome, "parked")
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertIsNone(state.get("pre_dev_fix_sha"))
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])


class ValidatingInterruptedResumeHandlerTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Handler-level guards: an interrupted resume in `_handle_validating`'s
    user-content-change and awaiting-human paths must NOT persist the
    consumption pre-staged before the spawn, so the next tick retries the
    resume rather than treating the input as already handled."""

    def test_content_change_resume_not_persisted(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(8, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=FOLLOWUP_COMMENT_ID,
                body="tweak the wording",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        gh.add_issue(issue)
        # A stale hash forces `_detect_user_content_change` to report drift
        # and route into the user-content-change resume.
        gh.seed_state(
            8,
            user_content_hash="stale-hash-forces-drift",
            last_action_comment_id=PICKUP_COMMENT_ID,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_number=SECONDARY_PR,
            branch=_issue_branch(8),
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                interrupted=True,
                last_message="partial drift fix before the shutdown SIGTERM",
            ),
            head_shas=[PRE_FIX_SHA],
        )

        # The dev resume DID run (so this exercises the post-resume guard),
        # but produced no commit and was killed.
        mocks[RUN_AGENT].assert_called_once()
        mocks[PUSH_BRANCH].assert_not_called()
        # Nothing persisted this tick: the seeded state stands untouched, so
        # the next tick re-detects the drift and retries the resume.
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])
        state = gh.pinned_data(8)
        self.assertEqual(state.get(USER_CONTENT_HASH), "stale-hash-forces-drift")
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), PICKUP_COMMENT_ID)

    def test_human_interrupted_resume_not_persisted(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=HUMAN_RETRY_COMMENT_ID,
                body="please retry",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        gh.add_issue(issue)
        # Seed a matching content hash so `_detect_user_content_change`
        # returns None (no drift, no first-call persist) and the handler
        # reaches the awaiting-human resume path cleanly.
        prior_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            9,
            awaiting_human=True,
            last_action_comment_id=1000,
            user_content_hash=prior_hash,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_number=INTERRUPTED_RESUME_PR,
            branch=_issue_branch(9),
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                interrupted=True,
                last_message="partial fix before the shutdown SIGTERM",
            ),
            head_shas=[PRE_FIX_SHA],
        )

        mocks[RUN_AGENT].assert_called_once()
        mocks[PUSH_BRANCH].assert_not_called()
        # Nothing persisted: the park stays put and the human reply is
        # re-consumed next tick against a fresh dev session.
        self.assertEqual(gh.write_state_calls, 0)
        state = gh.pinned_data(9)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), 1000)


class _ResumeTrustFixtureMixin(_PatchedWorkflowMixin):
    def _seed_cap_park(self, gh, *, author, body):
        issue = make_issue(TRUST_CAP_ISSUE, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body=body,
                user=FakeUser(author),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            TRUST_CAP_ISSUE,
            awaiting_human=True,
            park_reason=REVIEW_CAP,
            last_action_comment_id=ACTION_COMMENT_ID,
            review_round=config.MAX_REVIEW_ROUNDS,
            dev_session_id=DEV_SESSION,
            dev_agent=BACKEND_CODEX,
            pr_number=CAP_REASON_PR,
            branch=_issue_branch(TRUST_CAP_ISSUE),
        )
        return issue

    def _seed_reviewer_timeout_park(self, gh, *, author):
        issue = make_issue(TRUST_RETRY_ISSUE, label=LABEL_VALIDATING)
        issue.comments.append(
            FakeComment(
                id=FOLLOWUP_COMMENT_ID,
                body="please retry",
                user=FakeUser(author),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            TRUST_RETRY_ISSUE,
            awaiting_human=True,
            park_reason="reviewer_timeout",
            last_action_comment_id=ACTION_COMMENT_ID,
            review_round=1,
            dev_session_id=DEV_SESSION,
            dev_agent=BACKEND_CODEX,
            pr_number=SECONDARY_PR,
            branch=_issue_branch(TRUST_RETRY_ISSUE),
        )
        return issue


class HandleValidatingResumeTrustFilterTest(
    unittest.TestCase,
    _ResumeTrustFixtureMixin,
):
    """Allow only trusted authors to drive parked validating resumes."""

    def test_outsider_add_rounds_ignored(self) -> None:
        gh = FakeGitHubClient()
        issue = self._seed_cap_park(
            gh,
            author="mallory",
            body="/orchestrator add-review-rounds 5",
        )
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
            )
        # The outsider's command never reaches the parser: no reviewer rerun,
        # the cap and park stay put, and the watermark is not advanced past the
        # outsider comment so a later trusted command is still seen.
        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(TRUST_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        self.assertEqual(state.get(REVIEW_ROUND), config.MAX_REVIEW_ROUNDS)
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), ACTION_COMMENT_ID)
        self.assertFalse(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in gh.posted_comments
            )
        )

    def test_add_review_rounds_command_honored(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = self._seed_cap_park(
            gh,
            author="geserdugarov",
            body=ADD_ONE_ROUND_COMMAND,
        )
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=[BEFORE_FIX_SHA],
            )
        # A trusted operator's command resets the cap and reruns the reviewer
        # exactly as with no allowlist configured.
        state = gh.pinned_data(TRUST_CAP_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        self.assertTrue(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in gh.posted_comments
            )
        )

    def test_outsider_retry_does_not_respawn(self) -> None:
        gh = FakeGitHubClient()
        issue = self._seed_reviewer_timeout_park(gh, author="mallory")
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(),
            )
        # Filtered to empty, so the reviewer_timeout park self-heals through the
        # transient-recovery branch (as on a no-comment tick) instead of the
        # outsider's nudge waking the reviewer.
        mocks[RUN_AGENT].assert_not_called()

    def test_trusted_retry_respawns_reviewer(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = self._seed_reviewer_timeout_park(gh, author="geserdugarov")
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=[BEFORE_FIX_SHA],
            )
        # The trusted nudge re-spawns the reviewer this tick and advances the
        # watermark past the consumed comment.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        reviewer_spawns = [
            event
            for event in gh.recorded_events
            if event[EVENT_NAME] == EVENT_AGENT_SPAWN and event.get(AGENT_ROLE) == ROLE_REVIEWER
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(gh.pinned_data(TRUST_RETRY_ISSUE).get(LAST_ACTION_COMMENT_ID), FOLLOWUP_COMMENT_ID)


if __name__ == "__main__":
    unittest.main()
