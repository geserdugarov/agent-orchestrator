# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing session behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
COMMAND_COMMENT_ID = support.COMMAND_COMMENT_ID
CONTINUE_COMMAND = support.CONTINUE_COMMAND
DAVE = support.DAVE
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
DOCUMENTING = support.DOCUMENTING
EARLIER_PENDING_FIX_AT_TS = support.EARLIER_PENDING_FIX_AT_TS
FRESH_SESSION = support.FRESH_SESSION
FakeComment = support.FakeComment
FakeUser = support.FakeUser
ISSUE = support.ISSUE
PARK_AGENT_SILENT = support.PARK_AGENT_SILENT
PARK_REASON = support.PARK_REASON
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PUSHED_FIX_MESSAGE = support.PUSHED_FIX_MESSAGE
PUSHED_MESSAGE = support.PUSHED_MESSAGE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
TRIGGER_ID = support.TRIGGER_ID
VALIDATING = support.VALIDATING
_StrandedFixingFixtureMixin = support._StrandedFixingFixtureMixin
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
posted_comment_contains = support.posted_comment_contains
timedelta = support.timedelta
timezone = support.timezone


def _assert_retryable_limit_park(
    test_case,
    scenario,
    pinned_state,
    hitl_comment_text,
) -> None:
    test_case.assertTrue(pinned_state.get(AWAITING_HUMAN))
    test_case.assertEqual(
        pinned_state.get(PARK_REASON),
        PARK_AGENT_SILENT,
    )
    test_case.assertNotIn(
        (ISSUE, VALIDATING),
        scenario.github.label_history,
    )
    test_case.assertIn("session/usage limit", hitl_comment_text)
    test_case.assertIn(CONTINUE_COMMAND, hitl_comment_text)
    test_case.assertNotIn(
        "needs your input to proceed",
        hitl_comment_text,
    )


def _assert_limit_retry_result(test_case, scenario) -> None:
    test_case._mocks[RUN_AGENT].assert_called_once()
    test_case._agent_call = test_case._mocks[RUN_AGENT].call_args
    test_case.assertIsNone(
        test_case._agent_call.kwargs.get(RESUME_SESSION_ID),
    )
    test_case.assertIn(
        "please fix the flaky test",
        test_case._agent_call.args[1],
    )
    test_case.assertFalse(
        posted_comment_contains(
            scenario.github,
            "needs your actual guidance",
        ),
    )
    test_case.assertIn(
        (ISSUE, VALIDATING),
        scenario.github.label_history,
    )
    final = scenario.github.pinned_data(ISSUE)
    test_case.assertFalse(final.get(AWAITING_HUMAN))
    test_case.assertIsNone(final.get(PARK_REASON))


class FixingSilentSessionRecoveryTest(
    unittest.TestCase,
    _StrandedFixingFixtureMixin,
):
    def test_agent_silent_failure_parks_in_fixing(self) -> None:
        # Dev returned empty `last_message` and no commit. The handler
        # routes through `_on_question`'s silent-failure branch, parks
        # with `park_reason=PARK_AGENT_SILENT`, and the silent-park
        # counter ticks so a future resume can drop a poisoned session.
        # Label MUST stay at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please fix the import order",
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
                    last_message="",
                    exit_code=1,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertTrue(pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(pinned_data.get(PARK_REASON), PARK_AGENT_SILENT)
        self.assertNotIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        # Silent-park streak counter ticked so the next resume can
        # drop the poisoned session after the configured threshold.
        self.assertGreaterEqual(
            int(pinned_data.get("silent_park_count") or 0),
            1,
        )

    def test_session_limit_continue_retries(
        self,
    ) -> None:
        # #705 regression, #699 shape: a Claude session-limit notice arrives
        # as a normal FINAL message (non-empty `last_message`) during a fixing
        # dev-resume. It must park as a RETRYABLE session-failure
        # (`agent_silent`), NOT a real agent question (`park_reason=None`) --
        # otherwise a later bare `/orchestrator continue` after the reset is
        # refused as "needs your actual guidance" instead of retrying.
        self._long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        self._trigger = FakeComment(
            id=TRIGGER_ID,
            body="please fix the flaky test",
            user=FakeUser(ALICE),
            created_at=self._long_ago,
        )
        self._pr = self._open_pr()
        seeded = self._seed(
            pr=self._pr,
            issue_comments=[self._trigger],
        )
        scenario = IssueScenario(*seeded)
        self._session_limit = "You've hit your session limit · resets 7pm (Asia/Novosibirsk)"

        # --- Tick 1: the session-limit resume parks retryably -------------
        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=self._session_limit,
                ),
                head_shas=(SHA_BEFORE, SHA_BEFORE),
            )

        pinned_state = scenario.github.pinned_data(ISSUE)
        hitl_comment_text = "\n".join(body for _, body in scenario.github.posted_comments)
        _assert_retryable_limit_park(
            self,
            scenario,
            pinned_state,
            hitl_comment_text,
        )

        # --- Tick 2: `/orchestrator continue` retries, does not refuse ----
        scenario.issue.comments.append(
            FakeComment(
                id=COMMAND_COMMENT_ID,
                body=CONTINUE_COMMAND,
                user=FakeUser(DAVE),
            ),
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

        _assert_limit_retry_result(self, scenario)

    def test_restart_resumes_feedback_from_watermarks(
        self,
    ) -> None:
        # Crash/restart contract: the orchestrator has no in-memory
        # state across ticks, so a `fixing` issue with pending feedback
        # in pinned state must drive the rescan entirely off the
        # persisted watermarks + bookmarks. Simulate it by leaving the
        # `pending_fix_*` bookmarks recorded by a prior in_review tick
        # but starting with no transient state (no `awaiting_human`,
        # no in-flight session); the rescan finds the triggering
        # comment past `pr_last_comment_id`, debounce expires, and the
        # dev resumes -- exactly as if the handler had never run before.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body="please fix the off-by-one",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[comment],
                # Bookmarks left by in_review when it routed; transient
                # state cleared as if the process just started up.
                extra_state={
                    AWAITING_HUMAN: False,
                    PENDING_FIX_AT: EARLIER_PENDING_FIX_AT_TS,
                    PENDING_FIX_ISSUE_MAX_ID: TRIGGER_ID,
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

        self._mocks[RUN_AGENT].assert_called_once()
        # The followup quotes the triggering comment, proving the
        # rescan re-derived the unread feedback from the persisted
        # watermarks rather than relying on in-memory state.
        self._agent_call = self._mocks[RUN_AGENT].call_args
        prompt = self._agent_call.args[1]
        self.assertIn("please fix the off-by-one", prompt)
        # Push succeeded -> validating directly (the reviewer
        # re-evaluates the new head next tick); bookmarks cleared.
        self.assertIn((ISSUE, VALIDATING), scenario.github.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), scenario.github.label_history)
        self._pinned_data = scenario.github.pinned_data(ISSUE)
        self.assertIsNone(self._pinned_data.get(PENDING_FIX_AT))
        self.assertIsNone(self._pinned_data.get(PENDING_FIX_ISSUE_MAX_ID))
