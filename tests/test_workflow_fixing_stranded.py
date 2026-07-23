# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing stranded behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
CONTINUE_WORD = support.CONTINUE_WORD
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
FakeComment = support.FakeComment
FakeUser = support.FakeUser
IN_REVIEW = support.IN_REVIEW
ISSUE = support.ISSUE
MagicMock = support.MagicMock
NOTHING_TO_DO_MESSAGE = support.NOTHING_TO_DO_MESSAGE
PARK_PUSH_FAILED = support.PARK_PUSH_FAILED
PARK_REASON = support.PARK_REASON
PENDING_FIX_AT = support.PENDING_FIX_AT
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSH_BRANCH = support.PUSH_BRANCH
REVIEW_ROUND = support.REVIEW_ROUND
SHA_BEFORE = support.SHA_BEFORE
SHA_SAME = support.SHA_SAME
TRIGGER_ID = support.TRIGGER_ID
VALIDATING = support.VALIDATING
_StrandedFixingFixtureMixin = support._StrandedFixingFixtureMixin
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
timedelta = support.timedelta
timezone = support.timezone


class StrandedFixRecoveryTest(
    unittest.TestCase,
    _StrandedFixingFixtureMixin,
):
    def test_no_commit_with_stranded_fix_publishes_it(self) -> None:
        # The resume produced no new commit, but the clean worktree HEAD
        # is ahead of the remote PR branch: a prior parked run committed
        # a fix that was never pushed. The handler must publish it and
        # flip back to `validating` (bumping the round -- validating
        # route) instead of parking on a question the dev cannot answer.
        gh, issue = self._seed_stranded()

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="nothing new to commit; the fix is already on HEAD",
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
                branch_ahead_behind=(1, 0),
            )

        mocks[PUSH_BRANCH].assert_called_once()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertFalse(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 3)
        self.assertIn((ISSUE, VALIDATING), gh.label_history)

    def test_stranded_fix_behind_remote_parks(self) -> None:
        # Remote PR branch moved past our local view (behind > 0):
        # pushing would race a head we have not reconciled, so the
        # handler must fall back to the question park.
        gh, issue = self._seed_stranded()

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=NOTHING_TO_DO_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
                branch_ahead_behind=(1, 2),
            )

        mocks[PUSH_BRANCH].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)

    def test_stranded_fix_fetch_error_parks(self) -> None:
        # The pre-push fetch failed; without a current view of the
        # remote PR head the ahead/behind comparison is meaningless, so
        # the handler must not push and falls back to the question park.
        gh, issue = self._seed_stranded()

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=NOTHING_TO_DO_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
                branch_ahead_behind=(1, 0),
                authed_fetch_result=MagicMock(returncode=1, stderr="boom"),
            )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertTrue(gh.pinned_data(ISSUE).get(AWAITING_HUMAN))

    def test_no_commit_stranded_fix_dirty_tree_parks(self) -> None:
        # Stray uncommitted files alongside the stranded commit: pushing
        # only the commit would publish an incomplete branch (the exact
        # shape the dirty-park guard exists for), so the handler must
        # keep the question park.
        gh, issue = self._seed_stranded()

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=NOTHING_TO_DO_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
                branch_ahead_behind=(1, 0),
                dirty_files=("AGENTS.md",),
            )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertTrue(gh.pinned_data(ISSUE).get(AWAITING_HUMAN))

    def test_stranded_fix_push_error_parks_transient(self) -> None:
        # The deferred publish reuses the shared push tail, so a failed
        # push lands the standard `push_failed` transient park (which the
        # next tick's silent recovery can retry).
        gh, issue = self._seed_stranded()

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=NOTHING_TO_DO_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
                branch_ahead_behind=(1, 0),
                push_branch=False,
            )

        mocks[PUSH_BRANCH].assert_called_once()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_PUSH_FAILED)
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)

    def test_ack_stranded_fix_publishes(self) -> None:
        # in_review route (`pending_fix_at` set): the dev ACKs a no-commit
        # resume, but the clean worktree HEAD is strictly ahead of the
        # remote PR branch -- a fix a prior parked run committed that
        # never reached the PR (e.g. a dirty-park whose stray files were
        # later cleaned up). The ACK fast path must stand down: returning
        # to `in_review` would clear the bookmarks and advance the
        # watermarks while the PR head still lacks the fix. The handler
        # publishes the stranded HEAD through the normal push tail and
        # routes to `validating` with the in_review-route round reset.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=CONTINUE_WORD,
            user=FakeUser(ALICE),
            created_at=old,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=(
                        "The branch already satisfies the comment.\n\n"
                        "ACK: nothing to fix; the change is already on HEAD"
                    ),
                ),
                head_shas=(SHA_SAME, SHA_SAME),
                branch_ahead_behind=(1, 0),
            )

        mocks[PUSH_BRANCH].assert_called_once()
        self.assertNotIn((ISSUE, IN_REVIEW), scenario.github.label_history)
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertFalse(self._pinned_data.get(AWAITING_HUMAN))
        # in_review route: a pushed fix starts a fresh review cycle.
        self.assertEqual(self._pinned_data.get(REVIEW_ROUND), 0)
        self.assertIsNone(self._pinned_data.get(PENDING_FIX_AT))
        # Watermark advanced past the consumed feedback.
        self.assertGreaterEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)

    def test_behind_remote_ack_keeps_in_review(self) -> None:
        # The remote PR branch moved past the local view (behind > 0):
        # `_stranded_fix_unpushed` is conservative and reports False
        # rather than racing a head we have not reconciled, so the ACK
        # fast path proceeds as before -- return to `in_review` without
        # pushing blind.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=CONTINUE_WORD,
            user=FakeUser(ALICE),
            created_at=old,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=(
                        "The branch already satisfies the comment.\n\nACK: nothing to fix; 'continue' names no defect"
                    ),
                ),
                head_shas=(SHA_SAME, SHA_SAME),
                branch_ahead_behind=(1, 2),
            )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertIn((ISSUE, IN_REVIEW), scenario.github.label_history)
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertFalse(scenario.github.pinned_data(ISSUE).get(AWAITING_HUMAN))
