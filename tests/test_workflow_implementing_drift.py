# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""User-content drift / resume behavior for `_handle_implementing`: body-hash
changes resume the dev session, HEAD-SHA deltas detect masked silent
failures on recovered worktrees, and the no-dev-session drift branches park
or fall through to a fresh spawn with the full implement prompt."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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
    def test_body_drift_resumes_dev_session_not_re_decompose(self) -> None:
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
            branch="orchestrator/issue-60",
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
        data = gh.pinned_data(60)
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))

    def test_no_dev_session_falls_through_to_fresh_spawn(self) -> None:
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
        data = gh.pinned_data(61)
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")


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

    def test_recovered_unpushed_commits_do_not_mask_empty_resume(
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
            branch="orchestrator/issue-850",
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
        data = gh.pinned_data(850)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")


class ImplementingDriftNoDevSessionRecoveredCommitsTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: when implementing drift fires with NO recorded
    dev session AND the worktree carries recovered unpushed commits, the
    handler must refuse to push those commits and open a PR -- no agent
    has seen the edited issue body. Park awaiting human and let the
    operator decide whether to discard the recovered work or accept it."""

    def test_drift_with_recovered_commits_and_no_session_parks(
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
            branch="orchestrator/issue-860",
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
        data = gh.pinned_data(860)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("never saw the edited requirements", last_comment)
        # New hash baseline persisted so subsequent ticks don't keep
        # re-firing the drift park on the same edit.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")

    def test_drift_no_session_no_recovered_commits_falls_through(
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


class ImplementingDriftAwaitingHumanNoDevSessionTest(
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

    def test_body_edit_only_clears_park_and_fresh_spawns(self) -> None:
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

        data = gh.pinned_data(1200)
        # The new hash is durably persisted -- the drift does NOT loop.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Park flags cleared so the fresh-spawn branch fired.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # The fresh implement prompt was used (NOT the resume-with-just-
        # comments prompt), so the dev sees the updated body.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        self.assertIn("updated requirements", prompt)
        # PR opened from the fresh spawn.
        self.assertEqual(len(gh.opened_prs), 1)

    def test_body_edit_with_new_comment_uses_full_implement_prompt(
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
        data = gh.pinned_data(1210)
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 500,
        )
