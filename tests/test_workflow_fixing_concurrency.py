# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing concurrency behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
BOB = support.BOB
CONCURRENT_COMMENT_ID = support.CONCURRENT_COMMENT_ID
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
FIX_FEEDBACK = support.FIX_FEEDBACK
FakeComment = support.FakeComment
FakeUser = support.FakeUser
ISSUE = support.ISSUE
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSHED_MESSAGE = support.PUSHED_MESSAGE
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
STALE_PRE_COMMENT_HASH = support.STALE_PRE_COMMENT_HASH
TRIGGER_ID = support.TRIGGER_ID
USER_CONTENT_HASH = support.USER_CONTENT_HASH
VALIDATING = support.VALIDATING
_FixingFixtureMixin = support._FixingFixtureMixin
_InjectCommentAfterCall = support._InjectCommentAfterCall
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
timedelta = support.timedelta
timezone = support.timezone
workflow = support.workflow


class FixingContentHashAndConcurrencyTest(
    unittest.TestCase,
    _FixingFixtureMixin,
):
    def test_consumed_comment_refreshes_content_hash(
        self,
    ) -> None:
        # When fixing feeds a fresh issue-thread comment to the dev,
        # the next tick's `_handle_validating` would otherwise see the
        # same comment as user-content drift (the hash covers title +
        # body + human issue-thread comments) and resume the dev a
        # second time on input it already handled. The hash must
        # advance with the consumption so the validating drift check
        # is a no-op on the next tick.
        from orchestrator.workflow_drift import _compute_user_content_hash

        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please fix the docstring",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[comment],
                extra_state={
                    # Stale hash from before the human comment landed.
                    USER_CONTENT_HASH: STALE_PRE_COMMENT_HASH,
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

        self._pinned_data = scenario.github.pinned_data(ISSUE)
        # Pushed successfully, flipped directly to validating.
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        # The stored hash matches the current computed hash, i.e. the
        # validating tick's `_detect_user_content_change` will be a
        # no-op rather than re-resuming the dev on the already-consumed
        # comment.
        from orchestrator.workflow_messages import _orchestrator_ids

        expected = _compute_user_content_hash(
            scenario.issue,
            _orchestrator_ids(
                workflow.PinnedState(data=dict(self._pinned_data)),
            ),
        )
        self.assertEqual(self._pinned_data.get(USER_CONTENT_HASH), expected)
        self.assertNotEqual(
            self._pinned_data.get(USER_CONTENT_HASH),
            STALE_PRE_COMMENT_HASH,
        )

    def test_failed_fix_refreshes_content_hash(self) -> None:
        # Symmetric guard for the failure path: the dev saw the
        # comment via the resume prompt even when the push failed,
        # so the hash baseline must move with the consumption.
        # Otherwise a later relabel out of `fixing` into a stage
        # that consults `_detect_user_content_change` would re-fire
        # on the same comment.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=FIX_FEEDBACK,
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[comment],
                extra_state={USER_CONTENT_HASH: STALE_PRE_COMMENT_HASH},
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(timed_out=True),
                head_shas=(SHA_BEFORE,),
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertNotEqual(
            pinned_data.get(USER_CONTENT_HASH),
            STALE_PRE_COMMENT_HASH,
        )

    def test_pushed_bump_keeps_concurrent_comment(
        self,
    ) -> None:
        # Race window: a human posts an issue-thread comment AFTER the
        # handler's rescan but BEFORE the post-push watermark advance.
        # The pushed-fix bump MUST NOT leap past the unseen comment;
        # otherwise the next in_review tick (after validating completes)
        # would skip the feedback and the in_review HITL ready-ping
        # could advertise the PR as ready for human merge over it. The
        # legacy in_review pushed-fix path had the same constraint and
        # advanced only to comments actually fed to the dev.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        triggering = FakeComment(
            id=TRIGGER_ID,
            body="please fix the bug",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        self._pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=self._pr, issue_comments=[triggering]))

        # Splice in a concurrent human comment with id higher than the
        # triggering one mid-handler so the bump's `latest_comment_id`
        # candidate would otherwise leap past it.
        concurrent = FakeComment(
            id=CONCURRENT_COMMENT_ID,
            body="actually also rename helper",
            user=FakeUser(BOB),
            created_at=long_ago,
        )
        fix_and_inject = _InjectCommentAfterCall(
            workflow._handle_dev_fix_result,
            scenario.issue,
            concurrent,
        )

        with (
            patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS),
            patch.object(
                workflow,
                "_handle_dev_fix_result",
                fix_and_inject,
            ),
        ):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=PUSHED_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        self._pinned_data = scenario.github.pinned_data(ISSUE)
        # Label flipped to validating (push succeeded; reviewer
        # re-evaluates the new head next tick).
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        # Watermark advanced past the consumed triggering comment but
        # NOT past the concurrent one -- the next in_review tick must
        # still see the concurrent comment as fresh feedback.
        self.assertGreaterEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)
        self.assertLess(self._pinned_data.get(PR_LAST_COMMENT_ID), CONCURRENT_COMMENT_ID)

    def test_failed_bump_keeps_concurrent_comment(
        self,
    ) -> None:
        # Symmetric guard for the failure path: a human posts an
        # issue-thread comment AFTER the rescan but BEFORE the
        # post-park watermark advance. The bump MUST NOT leap past it;
        # otherwise the next fixing tick sees `awaiting_human` with no
        # new feedback, the gate fires, and the human's comment is
        # silently dropped. Verifies the "comments arriving while
        # already labeled `fixing`" contract on the timeout/dirty/push-
        # fail paths, mirroring the success-path guard above.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        triggering = FakeComment(
            id=TRIGGER_ID,
            body="please fix the bug",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        self._pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=self._pr, issue_comments=[triggering]))

        concurrent = FakeComment(
            id=CONCURRENT_COMMENT_ID,
            body="actually also rename helper",
            user=FakeUser(BOB),
            created_at=long_ago,
        )
        fail_and_inject = _InjectCommentAfterCall(
            workflow._handle_dev_fix_result,
            scenario.issue,
            concurrent,
        )

        with (
            patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS),
            patch.object(
                workflow,
                "_handle_dev_fix_result",
                fail_and_inject,
            ),
        ):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(timed_out=True),
                head_shas=(SHA_BEFORE,),
            )

        self._pinned_data = scenario.github.pinned_data(ISSUE)
        # Parked awaiting human (timeout failure).
        self.assertTrue(self._pinned_data.get(AWAITING_HUMAN))
        # Watermark advanced past the consumed triggering comment but
        # NOT past the concurrent one -- the next fixing tick must
        # still see the concurrent comment as fresh feedback.
        self.assertGreaterEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)
        self.assertLess(self._pinned_data.get(PR_LAST_COMMENT_ID), CONCURRENT_COMMENT_ID)

        # Second tick: rescan picks up the concurrent comment so
        # `awaiting_human and not new_feedback` is False; park flags
        # clear and the dev resumes with the human's text. Use a
        # successful agent result this time so the second tick
        # produces a push and we can assert the flow recovered.
        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=PUSHED_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        self._mocks[RUN_AGENT].assert_called_once()
        # The concurrent comment IS quoted in the next dev resume.
        self._agent_call = self._mocks[RUN_AGENT].call_args
        self._prompt = self._agent_call.args[1]
        self.assertIn("actually also rename helper", self._prompt)
