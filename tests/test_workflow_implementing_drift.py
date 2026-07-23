# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing drift behavior."""

from __future__ import annotations

import unittest

from tests import implementing_drift_test_support as support

AWAITING_HUMAN = support.AWAITING_HUMAN
DEV_AGENT = support.DEV_AGENT
DEV_SESSION = support.DEV_SESSION
DRIFT_RESUME_ISSUE = support.DRIFT_RESUME_ISSUE
FRESH_DRIFT_ISSUE = support.FRESH_DRIFT_ISSUE
FRESH_SESSION = support.FRESH_SESSION
FakeGitHubClient = support.FakeGitHubClient
HUMAN_COMMENT_ID = support.HUMAN_COMMENT_ID
IMPLEMENTED_MESSAGE = support.IMPLEMENTED_MESSAGE
IMPLEMENTER_PROMPT_FRAGMENT = support.IMPLEMENTER_PROMPT_FRAGMENT
INTERRUPTED_DRIFT_ISSUE = support.INTERRUPTED_DRIFT_ISSUE
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_VALIDATING = support.LABEL_VALIDATING
LAST_ACTION_COMMENT_ID = support.LAST_ACTION_COMMENT_ID
PICKUP_COMMENT_ID = support.PICKUP_COMMENT_ID
RECOVERED_COMMITS_ISSUE = support.RECOVERED_COMMITS_ISSUE
RUN_AGENT = support.RUN_AGENT
STALE_CONTENT_HASH = support.STALE_CONTENT_HASH
UPDATED_REQUIREMENTS = support.UPDATED_REQUIREMENTS
USER_CONTENT_HASH = support.USER_CONTENT_HASH
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_agent = support._agent
_issue_branch = support._issue_branch
make_issue = support.make_issue
posted_comment_contains = support.posted_comment_contains


def _assert_body_drift_outcome(test_case, github, prompt) -> None:
    test_case.assertIn(UPDATED_REQUIREMENTS, prompt)
    test_case.assertIn("Updated issue", prompt)
    test_case.assertNotIn(
        (DRIFT_RESUME_ISSUE, "decomposing"),
        github.label_history,
    )
    test_case.assertIn(
        (DRIFT_RESUME_ISSUE, LABEL_VALIDATING),
        github.label_history,
    )
    test_case.assertNotIn(
        (DRIFT_RESUME_ISSUE, "documenting"),
        github.label_history,
    )
    state = github.pinned_data(DRIFT_RESUME_ISSUE)
    test_case.assertNotEqual(
        state.get(USER_CONTENT_HASH),
        STALE_CONTENT_HASH,
    )
    test_case.assertTrue(
        posted_comment_contains(github, "issue body changed"),
    )


def _assert_interrupted_drift_state(
    test_case,
    github,
    before_writes,
) -> None:
    test_case.assertEqual(github.write_state_calls, before_writes)
    state = github.pinned_data(INTERRUPTED_DRIFT_ISSUE)
    test_case.assertEqual(
        state.get(USER_CONTENT_HASH),
        STALE_CONTENT_HASH,
    )
    test_case.assertTrue(state.get(AWAITING_HUMAN))
    test_case.assertEqual(
        state.get(LAST_ACTION_COMMENT_ID),
        HUMAN_COMMENT_ID,
    )
    test_case.assertEqual(github.opened_prs, [])
    test_case.assertNotIn(
        (INTERRUPTED_DRIFT_ISSUE, LABEL_VALIDATING),
        github.label_history,
    )
    test_case.assertFalse(
        posted_comment_contains(
            github,
            "agent needs your input",
        )
        or posted_comment_contains(
            github,
            "existing work satisfies",
        )
        or posted_comment_contains(github, "timed out"),
    )


class HandleImplementingResumeOnHashChangeTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_without_redecompose(self) -> None:
        # The spec rules out re-decomposing mid-implementation. Once a dev
        # session exists, the handler must instead notify the human and
        # resume the locked dev session with the new body so it can decide
        # whether more work is needed.
        gh = FakeGitHubClient()
        issue = make_issue(
            DRIFT_RESUME_ISSUE,
            label=LABEL_IMPLEMENTING,
            body=UPDATED_REQUIREMENTS,
        )
        gh.add_issue(issue)
        gh.seed_state(
            DRIFT_RESUME_ISSUE,
            user_content_hash=STALE_CONTENT_HASH,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            awaiting_human=True,
            last_action_comment_id=HUMAN_COMMENT_ID,
            branch=_issue_branch(DRIFT_RESUME_ISSUE),
        )

        self._mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="addressed it"),
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
        self._mocks[RUN_AGENT].assert_called_once()
        self._agent_args = self._mocks[RUN_AGENT].call_args[0]
        prompt = self._agent_args[1]
        _assert_body_drift_outcome(self, gh, prompt)

    def test_no_session_falls_through_to_fresh(self) -> None:
        # Pre-spawn implementing (ready -> implementing on the same tick,
        # but the dev hasn't run yet): a hash change should just persist
        # the new value and let the fresh-spawn path pick up the new body
        # via `_build_implement_prompt`. There is no "stale dev session"
        # to notify about.
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_DRIFT_ISSUE, label=LABEL_IMPLEMENTING, body="brand new body")
        gh.add_issue(issue)
        gh.seed_state(
            FRESH_DRIFT_ISSUE,
            user_content_hash=STALE_CONTENT_HASH,
            pickup_comment_id=PICKUP_COMMENT_ID,
        )

        self._mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(
                session_id=FRESH_SESSION,
                last_message=IMPLEMENTED_MESSAGE,
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
        self._agent_args = self._mocks[RUN_AGENT].call_args[0]
        prompt = self._agent_args[1]
        self.assertIn(IMPLEMENTER_PROMPT_FRAGMENT, prompt)
        # No "issue body changed" notice was posted (we fell through to
        # the normal fresh-spawn path).
        self.assertFalse(
            posted_comment_contains(gh, "issue body changed"),
        )
        # But the new hash is persisted.
        state = gh.pinned_data(FRESH_DRIFT_ISSUE)
        self.assertNotEqual(state.get(USER_CONTENT_HASH), STALE_CONTENT_HASH)


class ImplementingDriftInterruptedResumeTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """A user-content-change resume whose dev run the shutdown sweep killed
    mid-flight must be ignored: the handler returns WITHOUT writing pinned
    state, so the drift bookkeeping (consumed comments, refreshed
    `user_content_hash`) is discarded and the next process re-detects and
    re-runs the resume. It must NOT route through `_on_question` / the ack
    path / a timeout park off the partial result."""

    def test_interrupted_resume_keeps_state(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            INTERRUPTED_DRIFT_ISSUE,
            label=LABEL_IMPLEMENTING,
            body=UPDATED_REQUIREMENTS,
        )
        gh.add_issue(issue)
        gh.seed_state(
            INTERRUPTED_DRIFT_ISSUE,
            user_content_hash=STALE_CONTENT_HASH,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            awaiting_human=True,
            last_action_comment_id=HUMAN_COMMENT_ID,
            branch=_issue_branch(INTERRUPTED_DRIFT_ISSUE),
        )
        before_writes = gh.write_state_calls

        self._mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, interrupted=True),
            # before_sha + after_sha probes around the resume.
            head_shas=["before-resume", "after-resume"],
        )

        # The resume spawned, then the interruption was observed.
        self._mocks[RUN_AGENT].assert_called_once()
        _assert_interrupted_drift_state(self, gh, before_writes)


class ImplementingDriftHeadShaDeltaTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
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
            RECOVERED_COMMITS_ISSUE,
            label=LABEL_IMPLEMENTING,
            body=UPDATED_REQUIREMENTS,
        )
        gh.add_issue(issue)
        gh.seed_state(
            RECOVERED_COMMITS_ISSUE,
            user_content_hash=STALE_CONTENT_HASH,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            awaiting_human=True,
            last_action_comment_id=100,
            branch=_issue_branch(RECOVERED_COMMITS_ISSUE),
        )

        # The drift resume returns no new commit (`last_message=""` so
        # not an ack either -- this is a silent-failure shape). HEAD is
        # the same before and after, simulating a recovered worktree
        # carrying pre-existing unpushed commits from a prior tick: the
        # old SHA-agnostic `_has_new_commits` check would have returned
        # True (commits ahead of origin/base) and pushed a PR.
        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=""),
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
        self.assertNotIn((RECOVERED_COMMITS_ISSUE, LABEL_VALIDATING), gh.label_history)
        # Should fall to the silent-failure park via `_on_question`.
        state = gh.pinned_data(RECOVERED_COMMITS_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get("park_reason"), "agent_silent")
