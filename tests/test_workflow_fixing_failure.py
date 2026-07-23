# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing failure behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
DEV_SESSION_ID = support.DEV_SESSION_ID
DOCUMENTING = support.DOCUMENTING
FIX_FEEDBACK = support.FIX_FEEDBACK
FRESH_SESSION = support.FRESH_SESSION
FakeComment = support.FakeComment
FakeUser = support.FakeUser
ISSUE = support.ISSUE
PARK_PUSH_FAILED = support.PARK_PUSH_FAILED
PARK_REASON = support.PARK_REASON
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSHED_FIX_MESSAGE = support.PUSHED_FIX_MESSAGE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
TRIGGER_ID = support.TRIGGER_ID
VALIDATING = support.VALIDATING
_FixingFixtureMixin = support._FixingFixtureMixin
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
timedelta = support.timedelta
timezone = support.timezone


class FixingFailureDispositionTest(unittest.TestCase, _FixingFixtureMixin):
    def test_missing_dev_session_spawns_fresh(self) -> None:
        # `dev_session_id` may be absent on a `fixing` issue whose prior
        # dev session was dropped by the silent-park fallback, or on
        # legacy state that pre-dates session tracking. The fixing
        # handler MUST NOT park on missing-session: `_resume_dev_with_text`
        # treats `dev_sid=None` as the fresh-spawn case, so the dev
        # resumes correctly with the locked backend. Asserting fresh
        # spawn here pins the "resume correctly" half of the
        # crash/restart contract (the other half -- park on missing
        # `pr_number` -- is in `FixingLabelRoutingTest`).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please tighten the test",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[comment],
                extra_state={DEV_SESSION_ID: None},
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=FRESH_SESSION,
                    last_message=PUSHED_FIX_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        # The handler resumed with `resume_session_id=None` -- the locked
        # backend (`dev_agent=claude`) drives a fresh spawn rather than
        # parking on the missing session.
        self._mocks[RUN_AGENT].assert_called_once()
        call_args = self._mocks[RUN_AGENT].call_args
        self.assertIsNone(call_args.kwargs.get(RESUME_SESSION_ID))
        # Did NOT park -- the issue made progress instead (advancing
        # directly to validating for the reviewer to re-evaluate).
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertFalse(self._pinned_data.get(AWAITING_HUMAN))
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)

    def test_push_error_parks_with_transient_reason(self) -> None:
        # Push failure on the dev fix -> park awaiting_human in `fixing`
        # with the transient `push_failed` reason. The workflow label
        # MUST stay at `fixing` so the operator can see where the issue
        # is in the lifecycle; flipping to `validating` would imply the
        # fix landed when it did not.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=FIX_FEEDBACK,
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        self._pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=self._pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="fixed",
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
                push_branch=False,
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_PUSH_FAILED)
        # Label stayed at `fixing` -- no relabel to `validating`.
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        # Watermark advanced past the consumed feedback so the next
        # fixing tick does not replay it on top of the park.
        self.assertGreaterEqual(pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)

    def test_dirty_tree_parks_in_fixing(self) -> None:
        # Dev committed but left the tree dirty -> park (refuses to
        # push an incomplete branch). Label stays at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please rename helper",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="WIP",
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
                dirty_files=["orchestrator/foo.py"],
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        # `_on_dirty_worktree` clears `park_reason` (terminal, needs
        # human reply); the audit event still records the reason.
        self.assertIsNone(pinned_data.get(PARK_REASON))
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        # Watermark advanced past the consumed feedback.
        self.assertGreaterEqual(pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)

    def test_no_commit_question_parks_in_fixing(self) -> None:
        # Dev returned a clarifying question with no new commit. The
        # handler routes through `_on_question`, which parks
        # awaiting_human and posts the agent's text on the issue
        # thread. Label MUST stay at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please address the lint",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="Should I prefer ruff or black for this?",
                ),
                # No new commit: head_sha unchanged between before/after.
                head_shas=(SHA_BEFORE, SHA_BEFORE),
            )

        self._pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(self._pinned_data.get(AWAITING_HUMAN))
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        # Agent's question was surfaced to the human.
        self._joined = "\n".join(comment_body for _, comment_body in scenario.github.posted_comments)
        self.assertIn(
            "Should I prefer ruff or black for this?",
            self._joined,
        )
