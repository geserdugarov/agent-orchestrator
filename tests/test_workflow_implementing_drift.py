# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""User-content drift / resume behavior for `_handle_implementing`: body-hash
changes resume the dev session, HEAD-SHA deltas detect masked silent
failures on recovered worktrees, and the no-dev-session drift branches park
or fall through to a fresh spawn with the full implement prompt."""
from __future__ import annotations

import unittest

from orchestrator import workflow

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


class HandleImplementingResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_without_redecompose(self) -> None:
        # The spec rules out re-decomposing mid-implementation. Once a dev
        # session exists, the handler must instead notify the human and
        # resume the locked dev session with the new body so it can decide
        # whether more work is needed.
        gh = FakeGitHubClient()
        issue = make_issue(60, label="implementing", body="new requirements")
        gh.add_issue(issue)
        gh.seed_state(
            60,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=500,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-60",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="addressed it"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            # Two SHAs so the drift branch's "did THIS resume commit?"
            # head-SHA delta check sees a real change (the original
            # `_has_new_commits` check would have falsely accepted
            # pre-existing unpushed commits on a recovered worktree).
            head_shas=["before-resume", "after-resume"],
        )

        # Dev session resumed; the prompt mentions the updated body.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("new requirements", prompt)
        self.assertIn("Updated issue", prompt)
        # The label flipped via _on_commits -> validating because the
        # resume produced a commit; the issue is NOT routed to
        # decomposing, and the docs pass only runs as the final-docs
        # handoff after a reviewer approval.
        self.assertNotIn((60, "decomposing"), gh.label_history)
        self.assertIn((60, "validating"), gh.label_history)
        self.assertNotIn((60, "documenting"), gh.label_history)
        state = gh.pinned_data(60)
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))

    def test_no_session_falls_through_to_fresh(self) -> None:
        # Pre-spawn implementing (ready -> implementing on the same tick,
        # but the dev hasn't run yet): a hash change should just persist
        # the new value and let the fresh-spawn path pick up the new body
        # via `_build_implement_prompt`. There is no "stale dev session"
        # to notify about.
        gh = FakeGitHubClient()
        issue = make_issue(61, label="implementing", body="brand new body")
        gh.add_issue(issue)
        gh.seed_state(
            61,
            user_content_hash="stale-hash",
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) the drift-no-session
            # "are there recovered commits to park on?" check
            # (False -- fall through), (2) the regular fresh-spawn-
            # branch's "recovered worktree?" check (False), (3) the
            # post-agent "did the spawn commit?" check (True).
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh spawn ran; the implement prompt was built (not the
        # "issue body changed" resume prompt).
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        # No "issue body changed" notice was posted (we fell through to
        # the normal fresh-spawn path).
        self.assertFalse(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        # But the new hash is persisted.
        state = gh.pinned_data(61)
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")


class ImplementingDriftInterruptedResumeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """A user-content-change resume whose dev run the shutdown sweep killed
    mid-flight must be ignored: the handler returns WITHOUT writing pinned
    state, so the drift bookkeeping (consumed comments, refreshed
    `user_content_hash`) is discarded and the next process re-detects and
    re-runs the resume. It must NOT route through `_on_question` / the ack
    path / a timeout park off the partial result."""

    def test_interrupted_resume_keeps_state(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(62, label="implementing", body="new requirements")
        gh.add_issue(issue)
        gh.seed_state(
            62,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=500,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-62",
        )
        before_writes = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", interrupted=True),
            # before_sha + after_sha probes around the resume.
            head_shas=["before-resume", "after-resume"],
        )

        # The resume spawned, then the interruption was observed.
        mocks["run_agent"].assert_called_once()
        # No durable state churn -- the refreshed hash / consumed-comment /
        # awaiting-human writes are all discarded.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(62)
        self.assertEqual(state.get("user_content_hash"), "stale-hash")
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("last_action_comment_id"), 500)
        # No PR, no label flip, and no HITL question / ack / timeout park.
        self.assertEqual(gh.opened_prs, [])
        self.assertNotIn((62, "validating"), gh.label_history)
        self.assertFalse(any(
            "agent needs your input" in body
            or "existing work satisfies" in body
            or "timed out" in body
            for _, body in gh.posted_comments
        ))


class ImplementingDriftHeadShaDeltaTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: the implementing drift branch must compare HEAD
    SHA before/after the resume, not `_has_new_commits` (which only
    compares against `origin/<base>`). A worktree carrying pre-existing
    unpushed commits from a previous tick would otherwise mask an empty
    or failed resume and walk into `_on_commits` -> push -> open PR
    against commits that never had a chance to address the edited
    requirements."""

    def test_recovered_commits_expose_empty_resume(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            850, label="implementing", body="new requirements",
        )
        gh.add_issue(issue)
        gh.seed_state(
            850,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=100,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-850",
        )

        # The drift resume returns no new commit (`last_message=""` so
        # not an ack either -- this is a silent-failure shape). HEAD is
        # the same before and after, simulating a recovered worktree
        # carrying pre-existing unpushed commits from a prior tick: the
        # old SHA-agnostic `_has_new_commits` check would have returned
        # True (commits ahead of origin/base) and pushed a PR.
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message=""
            ),
            # has_new_commits would return True for the recovered
            # worktree; the drift branch must NOT consult it.
            has_new_commits=True,
            push_branch=True,
            head_shas=["recovered-sha", "recovered-sha"],
        )

        # The handler must NOT have opened a PR or flipped to
        # validating: the empty resume gave the dev no chance to
        # address the edited requirements.
        self.assertEqual(gh.opened_prs, [])
        self.assertNotIn((850, "validating"), gh.label_history)
        # Should fall to the silent-failure park via `_on_question`.
        state = gh.pinned_data(850)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("park_reason"), "agent_silent")


class NoSessionRecoveredCommitsDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: when implementing drift fires with NO recorded
    dev session AND the worktree carries recovered unpushed commits, the
    handler must refuse to push those commits and open a PR -- no agent
    has seen the edited issue body. Park awaiting human and let the
    operator decide whether to discard the recovered work or accept it."""

    def test_recovered_commits_without_session_park(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            860, label="implementing", body="updated requirements",
        )
        gh.add_issue(issue)
        # No `dev_session_id` recorded: legacy/recovered state. Pre-seed
        # `user_content_hash` so the drift detection fires (vs. silently
        # initializing the baseline on first encounter).
        gh.seed_state(
            860,
            user_content_hash="stale-hash",
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-860",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            # Recovered worktree has unpushed commits ahead of base.
            has_new_commits=True,
            push_branch=True,
        )

        # Crucial: must NOT push or open a PR against commits the dev
        # never authored against the edited body.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertNotIn((860, "validating"), gh.label_history)
        # Parked so the operator can adjudicate.
        state = gh.pinned_data(860)
        self.assertTrue(state.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("never saw the edited requirements", last_comment)
        # New hash baseline persisted so subsequent ticks don't keep
        # re-firing the drift park on the same edit.
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")

    def test_no_session_or_commits_falls_through(
        self,
    ) -> None:
        # The fall-through path is still correct when there are NO
        # recovered commits: a fresh spawn picks up the new body via
        # `_build_implement_prompt`.
        gh = FakeGitHubClient()
        issue = make_issue(861, label="implementing", body="new body")
        gh.add_issue(issue)
        gh.seed_state(
            861,
            user_content_hash="stale-hash",
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) drift-no-session park
            # check returns False -> fall through; (2) recovered-worktree
            # check in the regular path returns False; (3) post-agent
            # check returns True -> push + open PR.
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh implement prompt ran (not the drift resume prompt).
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        # PR opened from the fresh spawn.
        self.assertEqual(len(gh.opened_prs), 1)


class AwaitingHumanNoSessionDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point: implementing drift with no recorded `dev_session_id`
    can still be `awaiting_human=True` (manual relabel, drift on a
    freshly-picked-up issue parked before its first spawn, etc.).
    Without the fix:
      * body-edit-only: falls through to `_resume_developer_on_human_reply`,
        finds no new comments, returns -- and the new hash is never
        written, so the drift loops every tick.
      * with new comment: fresh-spawns via `_resume_dev_with_text` with
        ONLY the new-comment text as the prompt, never quoting the
        updated body that triggered the drift.
    Fix: clear the park flags so the fresh-spawn path below fires with
    the full implement prompt (which quotes `issue.body` and the
    conversation via `_recent_comments_text`)."""

    def test_body_edit_clears_park_and_spawns_fresh(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1200, label="implementing", body="updated requirements",
        )
        # No prior dev session, but parked. Pre-seed `user_content_hash`
        # to a stale value so the drift detection fires (auto-seeding on
        # first encounter would hide the bug).
        gh.seed_state(
            1200,
            user_content_hash="stale-hash",
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) the drift-no-session
            # park-on-recovered-commits check returns False; (2) the
            # else-branch recovered-worktree check returns False;
            # (3) the post-agent commit detection returns True.
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        state = gh.pinned_data(1200)
        # The new hash is durably persisted -- the drift does NOT loop.
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Park flags cleared so the fresh-spawn branch fired.
        self.assertFalse(state.get("awaiting_human"))
        self.assertIsNone(state.get("park_reason"))
        # The fresh implement prompt was used (NOT the resume-with-just-
        # comments prompt), so the dev sees the updated body.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        self.assertIn("updated requirements", prompt)
        # PR opened from the fresh spawn.
        self.assertEqual(len(gh.opened_prs), 1)

    def test_new_comment_body_edit_uses_full_prompt(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1210, label="implementing", body="updated body",
        )
        # New human comment that triggers comment-driven resume in the
        # legacy code path -- the bug there fresh-spawns with ONLY the
        # comment text, missing the body context.
        human = FakeComment(
            id=500, body="here's more detail",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        gh.seed_state(
            1210,
            user_content_hash="stale-hash",
            awaiting_human=True,
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh implement prompt with the updated body AND the new
        # comment quoted via `_recent_comments_text`.
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        self.assertIn("updated body", prompt)
        self.assertIn("here's more detail", prompt)
        # Comment marked consumed so the validating->in_review handoff
        # later won't classify it as fresh PR feedback.
        state = gh.pinned_data(1210)
        self.assertGreaterEqual(
            int(state.get("last_action_comment_id")), 500,
        )


class ImplementingContinueCommandTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """`/orchestrator continue` on a parked `implementing` issue is an
    operator command, not requirements drift (issue #729, the #720 shape). A
    retryable session-failure park retries the dev intentionally without a
    spurious "issue body changed" notice and without feeding the bare command
    as guidance; a park needing a real answer refuses; a command carrying real
    guidance falls through to the normal drift resume so the guidance drives
    the dev."""

    def _seed_parked(
        self, number: int, *, park_reason, command_body="/orchestrator continue",
        drift_neutral=False,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(number, label="implementing", body="the requirements")
        issue.comments.append(
            FakeComment(id=9000, body=command_body, user=FakeUser("dave"))
        )
        gh.add_issue(issue)
        # `drift_neutral` seeds the CURRENT content hash so drift is out of the
        # picture (the refuse path, not a drift no-op, is what's under test);
        # otherwise a stale hash proves the command handler intercepts BEFORE
        # drift detection would fire.
        content_hash = (
            workflow._compute_user_content_hash(issue, set())
            if drift_neutral else "stale-hash"
        )
        gh.seed_state(
            number,
            user_content_hash=content_hash,
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            park_reason=park_reason,
            silent_park_count=1,
            last_action_comment_id=8000,
            branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{number}",
        )
        return gh, issue

    def test_bare_continue_retries_without_notice(
        self,
    ) -> None:
        # The #720 shape: parked `agent_silent`, stale watermark, human posts
        # exactly `/orchestrator continue`. The dev session is resumed
        # intentionally -- no "issue body changed" / "issue content changed"
        # notice, and the bare command is NOT fed as the dev prompt.
        gh, issue = self._seed_parked(730, park_reason="agent_silent")

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="finished it"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["sha-before", "sha-after"],
        )

        # The dev retry/resume path is entered -- the poisoned but healthy
        # session is resumed (not rotated), on the neutral retry prompt.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("session/usage limit", prompt)
        self.assertNotIn("/orchestrator continue", prompt)
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "dev-sess",
        )
        # No drift notice of any kind.
        self.assertFalse(any(
            "issue body changed" in body or "issue content changed" in body
            for _, body in gh.posted_comments
        ))
        # The retry produced a commit, so the issue advanced to validating and
        # the command comment is consumed (won't re-fire next tick).
        self.assertIn((730, "validating"), gh.label_history)
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertEqual(gh.pinned_data(730).get("last_action_comment_id"), 9000)

    def test_question_park_bare_continue_refuses(
        self,
    ) -> None:
        # A real agent question parks with `park_reason=None`. A content-free
        # continue carries no answer, so refuse and stay parked -- and the
        # refusal must not re-post every tick.
        gh, issue = self._seed_parked(731, park_reason=None, drift_neutral=True)

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        # Second tick with no new human comment must not re-refuse or resume.
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        refusals = [
            body for _, body in gh.posted_comments
            if "needs your actual guidance" in body
        ]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(gh.opened_prs, [])
        state = gh.pinned_data(731)
        self.assertTrue(state.get("awaiting_human"))
        # Command AND the refusal are consumed so nothing re-fires.
        self.assertGreaterEqual(
            int(state.get("last_action_comment_id")), 9000,
        )

    def test_guided_continue_keeps_guidance(self) -> None:
        # A `/orchestrator continue` posted ALONGSIDE real guidance is not a
        # bare command: it falls through to the normal drift resume, which
        # feeds the guidance to the dev (it must not be dropped).
        gh, issue = self._seed_parked(
            732, park_reason="agent_silent",
            command_body="/orchestrator continue\nrename the flag to --strict",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="done"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["sha-before", "sha-after"],
        )

        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("rename the flag to --strict", prompt)
