# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing feedback behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
BOB = support.BOB
CAROL = support.CAROL
CHANGES_REQUESTED = support.CHANGES_REQUESTED
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
DOCUMENTING = support.DOCUMENTING
FIX_FEEDBACK = support.FIX_FEEDBACK
FOLLOWUP_ID = support.FOLLOWUP_ID
FRESH_COMMENT_DELAY_MINUTES = support.FRESH_COMMENT_DELAY_MINUTES
FakeComment = support.FakeComment
FakePRReview = support.FakePRReview
FakeUser = support.FakeUser
INLINE_FEEDBACK_ID = support.INLINE_FEEDBACK_ID
ISSUE = support.ISSUE
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PENDING_FIX_REVIEW_MAX_ID = support.PENDING_FIX_REVIEW_MAX_ID
PENDING_FIX_REVIEW_SUMMARY_MAX_ID = support.PENDING_FIX_REVIEW_SUMMARY_MAX_ID
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PR_LAST_REVIEW_COMMENT_ID = support.PR_LAST_REVIEW_COMMENT_ID
PR_LAST_REVIEW_SUMMARY_ID = support.PR_LAST_REVIEW_SUMMARY_ID
PUSHED_MESSAGE = support.PUSHED_MESSAGE
PUSH_BRANCH = support.PUSH_BRANCH
REVIEW_ROUND = support.REVIEW_ROUND
REVIEW_SUMMARY_FEEDBACK_ID = support.REVIEW_SUMMARY_FEEDBACK_ID
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


class FixingFeedbackRoutingTest(unittest.TestCase, _FixingFixtureMixin):
    def test_newer_comment_extends_debounce_window(self) -> None:
        # First tick: an older triggering comment is past the window but a
        # newer comment just landed -- the freshest
        # timestamp resets the gate. Handler must NOT resume; no agent
        # call, no label change.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        just_now = datetime.now(timezone.utc)
        triggering = FakeComment(
            id=TRIGGER_ID,
            body="please fix the bug",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        followup = FakeComment(
            id=FOLLOWUP_ID,
            body="actually rename it too",
            user=FakeUser(ALICE),
            created_at=just_now,
        )
        self._pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=self._pr,
                issue_comments=[triggering, followup],
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(),
            )

        self._mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(scenario.github.label_history, [])

    # --- comments arriving while already labeled fixing -------------------

    def test_fresh_comment_during_fixing_is_picked_up(self) -> None:
        # Tick 1 (in_review handoff already done; we simulate that state):
        # the triggering comment id=TRIGGER_ID sits past the watermark with the
        # bookmark recorded. Before tick 2 fires, a SECOND human comment
        # followup lands. The rescan picks BOTH up and the followup quotes
        # both surfaces. Both comments are past the debounce window.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        also_old = datetime.now(timezone.utc) - timedelta(minutes=FRESH_COMMENT_DELAY_MINUTES)
        triggering = FakeComment(
            id=TRIGGER_ID,
            body="please fix the docstring",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        late_arrival = FakeComment(
            id=FOLLOWUP_ID,
            body="and rename helper to util",
            user=FakeUser(BOB),
            created_at=also_old,
        )
        self._pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=self._pr,
                issue_comments=[triggering, late_arrival],
            )
        )

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
        self._agent_call = self._mocks[RUN_AGENT].call_args
        self._prompt = self._agent_call.args[1]
        # Both comments are quoted in the followup so the dev sees the
        # full conversation that landed while the label was `fixing`.
        self.assertIn("please fix the docstring", self._prompt)
        self.assertIn("and rename helper to util", self._prompt)
        # Watermark advanced past BOTH consumed comments.
        self.assertGreaterEqual(
            scenario.github.pinned_data(ISSUE).get(PR_LAST_COMMENT_ID),
            FOLLOWUP_ID,
        )

    # --- dev resume + push --> flip to validating ------------------------

    def test_pushed_fix_resets_and_enters_validating(self) -> None:
        # A pushed fix flips DIRECTLY back to `validating` so the
        # reviewer agent re-evaluates the freshened diff next tick.
        # Docs do not run on the pushed-fix exit -- the single docs
        # pass runs after reviewer approval before `in_review` via the
        # final-docs handoff, so running the docs stage against an
        # unapproved diff here would just push a no-op and waste a tick.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=FIX_FEEDBACK,
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="fixed",
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
                push_branch=True,
            )

        # Dev pushed; label flipped directly to validating.
        mocks[PUSH_BRANCH].assert_called_once()
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        # And NOT through documenting -- docs run after reviewer
        # approval before `in_review`, not on the pushed-fix exit.
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        # Review round reset so validating starts fresh on the new diff.
        self.assertEqual(self._pinned_data.get(REVIEW_ROUND), 0)
        # Bookmarks cleared after consumption.
        self.assertIsNone(self._pinned_data.get(PENDING_FIX_AT))
        self.assertIsNone(self._pinned_data.get(PENDING_FIX_ISSUE_MAX_ID))
        # Watermark advanced past the consumed comment.
        self.assertGreaterEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)

    def test_timeout_parks_and_advances_watermarks(self) -> None:
        # On dev timeout `_handle_dev_fix_result` parks awaiting human.
        # The fixing handler still advances the in_review watermarks past
        # the consumed feedback so the next tick does not replay it and
        # busy-loop the dev on the same comment.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please fix",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(*self._seed(pr=pr, issue_comments=[comment]))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(timed_out=True),
                head_shas=(SHA_BEFORE,),
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        # Watermark advanced even though no fix landed -- the dev saw
        # the feedback via the resume prompt.
        self.assertGreaterEqual(pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)
        # Did NOT advance to validating; stays in fixing for the
        # operator. (A pushed fix would relabel to validating.)
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)

    # --- watermark advancement across all three surfaces ----------------

    def test_pushed_fix_advances_all_three_watermarks(self) -> None:
        # Feedback lands on three surfaces simultaneously: an issue
        # comment, an inline review comment, and a review summary.
        # After a pushed fix every watermark must move past the max id
        # consumed on that surface.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue_comment = FakeComment(
            id=TRIGGER_ID,
            body="rename foo",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        inline_comment = FakeComment(
            id=INLINE_FEEDBACK_ID,
            body="add a test for this branch",
            user=FakeUser(BOB),
            created_at=long_ago,
        )
        summary_review = FakePRReview(
            id=REVIEW_SUMMARY_FEEDBACK_ID,
            body="please update the doc string",
            state=CHANGES_REQUESTED,
            user=FakeUser(CAROL),
            submitted_at=long_ago,
        )
        self._pr = self._open_pr(
            review_comments=[inline_comment],
            reviews=[summary_review],
        )
        scenario = IssueScenario(
            *self._seed(
                pr=self._pr,
                issue_comments=[issue_comment],
                extra_state={
                    PR_LAST_REVIEW_COMMENT_ID: INLINE_FEEDBACK_ID - 1,
                    PR_LAST_REVIEW_SUMMARY_ID: REVIEW_SUMMARY_FEEDBACK_ID - 1,
                    PENDING_FIX_REVIEW_MAX_ID: INLINE_FEEDBACK_ID,
                    PENDING_FIX_REVIEW_SUMMARY_MAX_ID: REVIEW_SUMMARY_FEEDBACK_ID,
                },
            )
        )

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

        self._mocks[PUSH_BRANCH].assert_called_once()
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertGreaterEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), TRIGGER_ID)
        self.assertEqual(
            self._pinned_data.get(PR_LAST_REVIEW_COMMENT_ID),
            INLINE_FEEDBACK_ID,
        )
        self.assertEqual(
            self._pinned_data.get(PR_LAST_REVIEW_SUMMARY_ID),
            REVIEW_SUMMARY_FEEDBACK_ID,
        )
        # Prompt also quoted every surface.
        self._agent_call = self._mocks[RUN_AGENT].call_args
        self._prompt = self._agent_call.args[1]
        self.assertIn("rename foo", self._prompt)
        self.assertIn("add a test for this branch", self._prompt)
        self.assertIn("please update the doc string", self._prompt)
