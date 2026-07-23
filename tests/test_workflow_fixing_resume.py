# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing resume behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
DOCUMENTING = support.DOCUMENTING
FakeComment = support.FakeComment
FakeUser = support.FakeUser
HUMAN_REPLY_ID = support.HUMAN_REPLY_ID
ISSUE = support.ISSUE
PARKED_COMMENT_WATERMARK = support.PARKED_COMMENT_WATERMARK
PARK_AGENT_TIMEOUT = support.PARK_AGENT_TIMEOUT
PARK_PUSH_FAILED = support.PARK_PUSH_FAILED
PARK_REASON = support.PARK_REASON
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PRE_DEV_FIX_SHA = support.PRE_DEV_FIX_SHA
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSHED_MESSAGE = support.PUSHED_MESSAGE
PUSH_BRANCH = support.PUSH_BRANCH
REVIEW_ROUND = support.REVIEW_ROUND
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
TEMP_ROOT = support.TEMP_ROOT
TRANSIENT_PARK_WATERMARK = support.TRANSIENT_PARK_WATERMARK
UNCHANGED_SHA = support.UNCHANGED_SHA
VALIDATING = support.VALIDATING
WORKTREE_PATH = support.WORKTREE_PATH
_FixingFixtureMixin = support._FixingFixtureMixin
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
timedelta = support.timedelta
timezone = support.timezone
workflow = support.workflow


class FixingAwaitingHumanResumeTest(unittest.TestCase, _FixingFixtureMixin):
    def test_no_new_feedback_is_noop(self) -> None:
        # After a prior failed tick parked the issue and bumped the
        # watermark past the original triggering comment, a poll with no
        # fresh human reply must be a no-op -- no agent spawn, no comment
        # post, no label change.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_AGENT_TIMEOUT,
                PR_LAST_COMMENT_ID: PARKED_COMMENT_WATERMARK,
            },
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_fresh_reply_resumes_dev(self) -> None:
        # The human typed a reply after the park. The fresh comment is
        # past the bumped watermark and past the debounce window, so the
        # handler clears the park flags and resumes the dev with the
        # new context.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="actually try X instead",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[reply],
                extra_state={
                    AWAITING_HUMAN: True,
                    PARK_REASON: PARK_AGENT_TIMEOUT,
                    PR_LAST_COMMENT_ID: PARKED_COMMENT_WATERMARK,
                },
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=PUSHED_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        mocks[RUN_AGENT].assert_called_once()
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        # Park flags cleared (either by _resume_dev_with_text or after
        # the successful push). After a successful push we end up in
        # validating directly so the reviewer re-evaluates the new
        # head next tick.
        self.assertFalse(self._pinned_data.get(AWAITING_HUMAN))
        self.assertIsNone(self._pinned_data.get(PARK_REASON))
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)

    def test_validating_fix_bumps_instead_of_resets(self) -> None:
        # A parked CHANGES_REQUESTED dev fix (label flipped to `fixing`
        # by `_handle_validating`) is finished off via a human reply.
        # The pushed fix must BUMP `review_round`, not reset it: we are
        # still inside the same review cycle (the previous reviewer
        # round was CHANGES_REQUESTED, not APPROVED) and resetting would
        # silently restart MAX_REVIEW_ROUNDS accounting.
        # `pending_fix_at` is the discriminator: in_review->fixing sets
        # it (and resets the round on push); validating->fixing does NOT
        # set it (and bumps the round on push).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="here's a clarification: use option B",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[reply],
                extra_state={
                    AWAITING_HUMAN: True,
                    PARK_REASON: PARK_AGENT_TIMEOUT,
                    PR_LAST_COMMENT_ID: PARKED_COMMENT_WATERMARK,
                    # validating->fixing route did NOT set pending_fix_at;
                    # only the in_review route sets it. Override the seed's
                    # default to model the validating-route shape.
                    PENDING_FIX_AT: None,
                    PENDING_FIX_ISSUE_MAX_ID: None,
                    REVIEW_ROUND: 2,
                },
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=PUSHED_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        # `review_round` bumped from 2 to 3 -- the review cycle continues
        # under MAX_REVIEW_ROUNDS rather than starting over at 0.
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 3)
        # Flipped back to validating so the reviewer re-evaluates next tick.
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)


class FixingTransientParkRecoveryTest(
    unittest.TestCase,
    _FixingFixtureMixin,
):
    def test_push_failure_park_recovers_on_success(
        self,
    ) -> None:
        # A `_handle_validating` CHANGES_REQUESTED dev fix can park
        # under `fixing` with `park_reason=PARK_PUSH_FAILED` after a
        # racing non-fast-forward push. Without the recovery branch
        # the issue would sit in `fixing` forever because
        # `_resume_developer_on_human_reply` only fires on a new human
        # comment AND the deferred --force-with-lease push that
        # eventually lands does not produce one. The recovery branch
        # silently retries the push and, on success, clears the park
        # flags, bumps `review_round`, and flips back to `validating`
        # so the reviewer re-evaluates the now-landed head.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_PUSH_FAILED,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                # Validating route did not set pending_fix_at.
                PENDING_FIX_AT: None,
                PENDING_FIX_ISSUE_MAX_ID: None,
                REVIEW_ROUND: 1,
            },
        )

        # `_worktree_path` is not mocked by the standard mixin (only
        # `_ensure_worktree` is). The recovery helper checks
        # `wt.exists()` before retrying the push, so route it to an
        # existing path. `/tmp` exists; the actual filesystem state
        # does not matter because `_push_branch` is mocked.
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

        # Recovery ran -- not a human-comment driven resume.
        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_called_once()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertFalse(pinned_data.get(AWAITING_HUMAN))
        self.assertIsNone(pinned_data.get(PARK_REASON))
        # Round bumped because a fix landed (the recovery helper bumps
        # on its `pushed` outcome).
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 2)
        # Flipped back to validating so the reviewer reruns next tick.
        self.assertIn((ISSUE, VALIDATING), gh.label_history)

    def test_push_failure_park_stays_on_failure(
        self,
    ) -> None:
        # The remote is still rejecting the push. The recovery branch
        # must leave the park in place (no flag clear, no relabel) and
        # NOT re-post the park comment -- the next tick retries.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_PUSH_FAILED,
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
                push_branch=False,
            )

        mocks[RUN_AGENT].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        # Park flags unchanged.
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_PUSH_FAILED)
        # Still on `fixing` (no relabel emitted this tick).
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
        # Did NOT re-post the park comment (would be repetitive churn).
        self.assertEqual(gh.posted_comments, [])

    def test_timeout_park_clears_without_commit(self) -> None:
        # An `agent_timeout` park with `pre_dev_fix_sha == head_sha` means
        # the timeout produced no new commit. The recovery branch clears
        # the park flags WITHOUT bumping the round (nothing landed) and
        # flips back to `validating` so the reviewer reruns. The dev
        # session is not respawned in fixing -- the next validating tick
        # re-runs the reviewer which decides whether the same
        # CHANGES_REQUESTED fix is still needed.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_AGENT_TIMEOUT,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                PENDING_FIX_AT: None,
                PENDING_FIX_ISSUE_MAX_ID: None,
                REVIEW_ROUND: 1,
                # before-SHA equals current HEAD -- timeout did not
                # commit. The mixin's `head_shas` controls `_head_sha`.
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
            )

        mocks[RUN_AGENT].assert_not_called()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertFalse(pinned_data.get(AWAITING_HUMAN))
        self.assertIsNone(pinned_data.get(PARK_REASON))
        # No round bump -- the timeout produced no fix.
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 1)
        self.assertIn((ISSUE, VALIDATING), gh.label_history)
        # `pre_dev_fix_sha` watermark cleared by the recovery helper so
        # a future park does not re-use a stale value.
        self.assertIsNone(pinned_data.get(PRE_DEV_FIX_SHA))

    def test_timeout_park_pushes_dev_commit(
        self,
    ) -> None:
        # The dev committed before the timeout killed it; recovery
        # pushes the new SHA and bumps `review_round`. Mirrors the
        # validating-side `pushed` branch.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: PARK_AGENT_TIMEOUT,
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                PENDING_FIX_AT: None,
                PENDING_FIX_ISSUE_MAX_ID: None,
                REVIEW_ROUND: 1,
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
                # HEAD moved past pre-agent SHA -- the dev had committed.
                head_shas=("bbb",),
                push_branch=True,
                dirty_files=(),
            )

        mocks[PUSH_BRANCH].assert_called_once()
        pinned_data = gh.pinned_data(ISSUE)
        self.assertFalse(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 2)
        self.assertIn((ISSUE, VALIDATING), gh.label_history)
