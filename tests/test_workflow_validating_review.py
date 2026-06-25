# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Optional

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-5",
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
        # The dev-fix subphase runs under the `fixing` label so the active
        # job is observably "fixing reviewer-requested changes" rather
        # than "validating". On a successful pushed fix the handler flips
        # back to `validating` so the reviewer re-evaluates the new head
        # on the next tick. No documenting hop -- the docs pass only runs
        # as the final-docs handoff after approval.
        self.assertIn((5, "fixing"), gh.label_history)
        # The trailing label entry must be `validating` so the next tick
        # picks up via `_handle_validating`.
        self.assertEqual(gh.label_history[-1], (5, "validating"))
        # The `fixing` flip happens BEFORE the `validating` flip so an
        # external observer sees the active work labeled `fixing` for the
        # duration of the dev subprocess.
        fixing_idx = gh.label_history.index((5, "fixing"))
        validating_idx = gh.label_history.index((5, "validating"))
        self.assertLess(fixing_idx, validating_idx)
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-6",
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
        # CHANGES_REQUESTED flips the label to `fixing` BEFORE the dev
        # spawn so a parked subprocess leaves the active job labeled
        # `fixing` (the fixing handler then owns the awaiting-human
        # rescan + dev resume cycle on subsequent ticks).
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertNotIn((6, "validating"), gh.label_history)

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
        # The pre-spawn label flip is observed even on the no-commit park
        # path (the fixing handler then handles the awaiting-human rescan
        # on the next tick).
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertNotIn((6, "validating"), gh.label_history)

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
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertNotIn((6, "validating"), gh.label_history)

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
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertNotIn((6, "validating"), gh.label_history)

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

    def test_changes_requested_flips_to_fixing_before_dev_spawn(self) -> None:
        # The dev-fix subphase must run under the `fixing` label so the
        # active job is observably "fixing reviewer-requested changes"
        # rather than "validating". The label flip lands BEFORE the dev
        # subprocess so an external observer never sees the dev work
        # labeled only `validating`; the `fixing` entry must therefore
        # appear in the label history strictly before any later flip
        # back to `validating`.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="fixed"),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Both flips landed in order: first `fixing` (pre-spawn), then
        # `validating` (post-push) so the reviewer reruns on the next tick.
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertIn((6, "validating"), gh.label_history)
        fixing_idx = gh.label_history.index((6, "fixing"))
        validating_idx = gh.label_history.index((6, "validating"))
        self.assertLess(fixing_idx, validating_idx)
        # The dev work is tagged with `stage="fixing"` for analytics so
        # spend on a CHANGES_REQUESTED fix is not double-counted against
        # the validating bucket alongside the reviewer/verify spend.
        dev_spawns = [
            e for e in gh.recorded_events
            if e["event"] == "agent_spawn"
            and e.get("agent_role") == "developer"
        ]
        self.assertEqual(len(dev_spawns), 1)
        self.assertEqual(dev_spawns[0]["stage"], "fixing")

    def test_dev_fix_interrupted_skips_write_and_does_not_push(self) -> None:
        # A shutdown-killed CHANGES_REQUESTED dev resume is ignored: the
        # handler does NOT persist the post-spawn state (so the per-session
        # resume budget `dev_resume_count` charged by `_resume_dev_with_text`
        # is not burned) and does NOT push. The pre-spawn `fixing` flip
        # stands; the next tick re-runs the cycle. Any local commit the
        # killed run left is republished by `_handle_dev_fix_result`'s
        # stranded-fix gate on the next clean resume, not this interrupted
        # one.
        gh, issue = self._seeded(dev_agent="claude", dev_session_id="dev-sess")
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(
                    session_id="dev-sess",
                    interrupted=True,
                    last_message="committed a partial fix before the SIGTERM",
                ),
            ],
            head_shas=["aaa"],
        )

        # Reviewer + dev resume both ran.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        # The interrupted run is not pushed.
        mocks["_push_branch"].assert_not_called()
        # Pre-spawn flip landed; the issue did NOT bounce to validating this
        # tick (that happens on a later tick after a clean re-review).
        self.assertIn((6, "fixing"), gh.label_history)
        self.assertNotIn((6, "validating"), gh.label_history)
        data = gh.pinned_data(6)
        # Post-spawn write skipped: the resume-budget charge from
        # `_resume_dev_with_text` never persisted.
        self.assertIsNone(data.get("dev_resume_count"))
        # Interrupted is not a question / timeout / dirty park.
        self.assertFalse(data.get("awaiting_human"))


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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-7",
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-70",
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-80",
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-81",
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-82",
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
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-83",
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


class ValidatingDevFixInterruptedHelperTest(unittest.TestCase):
    """The shared validating dev-fix helpers must ignore a shutdown-killed
    (interrupted) result: no park, no HITL comment, no push, no watermark,
    and no `_on_question`. The guard short-circuits before any of that, so
    these call the helpers directly without the worktree/git mocks."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(7, label="validating")
        gh.add_issue(issue)
        gh.seed_state(7, **state)
        return gh, gh.read_pinned_state(issue), issue

    def test_handle_dev_fix_result_interrupted_returns_false_no_side_effects(
        self,
    ) -> None:
        gh, state, issue = self._seeded()
        result = _agent(
            session_id="dev-sess",
            interrupted=True,
            last_message="partial output before the shutdown SIGTERM",
        )

        pushed = workflow._handle_dev_fix_result(
            gh, _TEST_SPEC, issue, state, Path("/tmp/wt"), result, "sha-before",
        )

        self.assertFalse(pushed)
        # No park: awaiting_human untouched, no transient reason tagged, no
        # timeout watermark persisted.
        self.assertFalse(state.get("awaiting_human"))
        self.assertIsNone(state.get("park_reason"))
        self.assertIsNone(state.get("pre_dev_fix_sha"))
        # No HITL / question comment posted on either surface.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])

    def test_post_user_content_change_result_interrupted_returns_parked(
        self,
    ) -> None:
        gh, state, issue = self._seeded()
        result = _agent(
            session_id="dev-sess",
            interrupted=True,
            last_message="ACK: looks fine",  # partial; must NOT be honored
        )

        outcome = workflow._post_user_content_change_result(
            gh, _TEST_SPEC, issue, state, Path("/tmp/wt"), result, "sha-before",
        )

        # Reported parked, but WITHOUT swallowing the partial message as an
        # ack or parking awaiting_human.
        self.assertEqual(outcome, "parked")
        self.assertFalse(state.get("awaiting_human"))
        self.assertIsNone(state.get("park_reason"))
        self.assertIsNone(state.get("pre_dev_fix_sha"))
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])


class ValidatingInterruptedResumeHandlerTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Handler-level guards: an interrupted resume in `_handle_validating`'s
    user-content-change and awaiting-human paths must NOT persist the
    consumption pre-staged before the spawn, so the next tick retries the
    resume rather than treating the input as already handled."""

    def test_user_content_change_interrupted_resume_does_not_persist(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(8, label="validating")
        issue.comments.append(
            FakeComment(id=1200, body="tweak the wording", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        # A stale hash forces `_detect_user_content_change` to report drift
        # and route into the user-content-change resume.
        gh.seed_state(
            8,
            user_content_hash="stale-hash-forces-drift",
            last_action_comment_id=900,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            pr_number=18,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-8",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                interrupted=True,
                last_message="partial drift fix before the shutdown SIGTERM",
            ),
            head_shas=["sha-before"],
        )

        # The dev resume DID run (so this exercises the post-resume guard),
        # but produced no commit and was killed.
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        # Nothing persisted this tick: the seeded state stands untouched, so
        # the next tick re-detects the drift and retries the resume.
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(8)
        self.assertEqual(data.get("user_content_hash"), "stale-hash-forces-drift")
        self.assertEqual(data.get("last_action_comment_id"), 900)

    def test_awaiting_human_interrupted_resume_does_not_persist(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label="validating")
        issue.comments.append(
            FakeComment(id=1300, body="please retry", user=FakeUser("alice"))
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
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            pr_number=19,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                interrupted=True,
                last_message="partial fix before the shutdown SIGTERM",
            ),
            head_shas=["sha-before"],
        )

        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        # Nothing persisted: the park stays put and the human reply is
        # re-consumed next tick against a fresh dev session.
        self.assertEqual(gh.write_state_calls, 0)
        data = gh.pinned_data(9)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("last_action_comment_id"), 1000)
