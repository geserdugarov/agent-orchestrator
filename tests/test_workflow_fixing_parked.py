# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing parked behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

AWAITING_HUMAN = support.AWAITING_HUMAN
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
ISSUE = support.ISSUE
PARK_AGENT_QUESTION = support.PARK_AGENT_QUESTION
PARK_AGENT_TIMEOUT = support.PARK_AGENT_TIMEOUT
PARK_PUSH_FAILED = support.PARK_PUSH_FAILED
PARK_REASON = support.PARK_REASON
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_AT_TS = support.PENDING_FIX_AT_TS
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PRE_DEV_FIX_SHA = support.PRE_DEV_FIX_SHA
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSH_BRANCH = support.PUSH_BRANCH
REVIEW_ROUND = support.REVIEW_ROUND
RUN_AGENT = support.RUN_AGENT
TEMP_ROOT = support.TEMP_ROOT
TRANSIENT_PARK_WATERMARK = support.TRANSIENT_PARK_WATERMARK
TRIGGER_ID = support.TRIGGER_ID
UNCHANGED_SHA = support.UNCHANGED_SHA
VALIDATING = support.VALIDATING
WORKTREE_PATH = support.WORKTREE_PATH
_FixingFixtureMixin = support._FixingFixtureMixin
_agent = support._agent
config = support.config
patch = support.patch
workflow = support.workflow


class FixingParkIsolationTest(unittest.TestCase, _FixingFixtureMixin):
    def test_review_timeout_park_not_recovered(
        self,
    ) -> None:
        # Regression: the transient recovery branch must NOT fire on
        # the in_review->fixing route (discriminator: `pending_fix_at`
        # is set). `_handle_fixing` advances the PR-feedback watermarks
        # past the human's comment on a timed-out resume so the dev
        # does not replay it; silently clearing `agent_timeout` here
        # would consume that human PR feedback without applying a fix
        # and bounce the issue back to `validating`, where the reviewer
        # would re-approve over unread human feedback.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_AGENT_TIMEOUT,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                # in_review route DID set this -- we are mid-fix on a
                # human PR comment.
                PENDING_FIX_AT: PENDING_FIX_AT_TS,
                PENDING_FIX_ISSUE_MAX_ID: TRIGGER_ID,
                REVIEW_ROUND: 0,
                # before-SHA equals current HEAD -- the timed-out dev
                # produced no commit. The shared helper would otherwise
                # report "cleared" and the handler would relabel back
                # to validating.
                PRE_DEV_FIX_SHA: UNCHANGED_SHA,
            },
        )

        with (
            patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS),
            patch.object(workflow, WORKTREE_PATH, return_value=TEMP_ROOT),
        ):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
                head_shas=(UNCHANGED_SHA,),
                push_branch=True,
            )

        # No recovery attempt: the dev was not respawned and no push
        # was attempted on the gated path.
        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        # Park flags preserved -- the route waits for a human comment.
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_AGENT_TIMEOUT)
        # Stayed on `fixing`; did NOT relabel.
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
        # Bookmark untouched so the in_review semantics survive into
        # the next tick after the human replies.
        self.assertEqual(
            pinned_data.get(PENDING_FIX_AT),
            PENDING_FIX_AT_TS,
        )

    def test_review_push_park_not_recovered(
        self,
    ) -> None:
        # Same gate, push_failed flavor: on the in_review route a
        # deferred --force-with-lease push must NOT be retried here
        # because the shared helper's `pushed` branch bumps
        # `review_round`, while the in_review route resets it to 0 on
        # the eventual push success (the previous reviewer round was
        # APPROVED). Letting the helper run would mis-account the
        # round under MAX_REVIEW_ROUNDS.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_PUSH_FAILED,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                PENDING_FIX_AT: PENDING_FIX_AT_TS,
                PENDING_FIX_ISSUE_MAX_ID: TRIGGER_ID,
                REVIEW_ROUND: 0,
            },
        )

        with (
            patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS),
            patch.object(workflow, WORKTREE_PATH, return_value=TEMP_ROOT),
        ):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        mocks[PUSH_BRANCH].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        # Park preserved; waits for human input.
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_PUSH_FAILED)
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
        # Bookmark and round unchanged.
        self.assertEqual(
            pinned_data.get(PENDING_FIX_AT),
            PENDING_FIX_AT_TS,
        )
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 0)

    def test_nontransient_park_stays_silent(self) -> None:
        # Park reasons that REQUIRE a human comment to unstick (an
        # agent question, a dirty worktree) must not be silently
        # recovered. The handler returns early as before.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                # Not a transient reason; the dev raised a question and
                # the human needs to answer.
                PARK_REASON: PARK_AGENT_QUESTION,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                PENDING_FIX_AT: None,
                PENDING_FIX_ISSUE_MAX_ID: None,
                REVIEW_ROUND: 1,
            },
        )

        with (
            patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS),
            patch.object(workflow, WORKTREE_PATH, return_value=TEMP_ROOT),
        ):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        # Unchanged park.
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_AGENT_QUESTION)
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 1)
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
