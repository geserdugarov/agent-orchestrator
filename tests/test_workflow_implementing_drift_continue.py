# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing drift continue behavior."""

from __future__ import annotations

import unittest

from tests import implementing_drift_test_support as support

AWAITING_HUMAN = support.AWAITING_HUMAN
COMMAND_COMMENT_ID = support.COMMAND_COMMENT_ID
CONTINUE_COMMAND = support.CONTINUE_COMMAND
CONTINUE_GUIDED_ISSUE = support.CONTINUE_GUIDED_ISSUE
CONTINUE_QUESTION_ISSUE = support.CONTINUE_QUESTION_ISSUE
CONTINUE_RETRY_ISSUE = support.CONTINUE_RETRY_ISSUE
DEV_SESSION = support.DEV_SESSION
IssueScenario = support.IssueScenario
LABEL_VALIDATING = support.LABEL_VALIDATING
LAST_ACTION_COMMENT_ID = support.LAST_ACTION_COMMENT_ID
RUN_AGENT = support.RUN_AGENT
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_agent = support._agent
_seed_parked_implementing = support._seed_parked_implementing


class ImplementingContinueCommandTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """`/orchestrator continue` on a parked `implementing` issue is an
    operator command, not requirements drift (issue #729, the #720 shape). A
    retryable session-failure park retries the dev intentionally without a
    spurious "issue body changed" notice and without feeding the bare command
    as guidance; a park needing a real answer refuses; a command carrying real
    guidance falls through to the normal drift resume so the guidance drives
    the dev."""

    def test_bare_continue_retries_without_notice(
        self,
    ) -> None:
        # The #720 shape: parked `agent_silent`, stale watermark, human posts
        # exactly `/orchestrator continue`. The dev session is resumed
        # intentionally -- no "issue body changed" / "issue content changed"
        # notice, and the bare command is NOT fed as the dev prompt.
        scenario = IssueScenario(
            *_seed_parked_implementing(
                CONTINUE_RETRY_ISSUE,
                park_reason="agent_silent",
            )
        )

        mocks = self._run_implementing(
            scenario.github,
            scenario.issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="finished it"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["sha-before", "sha-after"],
        )

        # The dev retry/resume path is entered -- the poisoned but healthy
        # session is resumed (not rotated), on the neutral retry prompt.
        mocks[RUN_AGENT].assert_called_once()
        prompt = mocks[RUN_AGENT].call_args[0][1]
        self.assertIn("session/usage limit", prompt)
        self.assertNotIn(CONTINUE_COMMAND, prompt)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs.get("resume_session_id"),
            DEV_SESSION,
        )
        # No drift notice of any kind.
        self.assertFalse(
            any(
                "issue body changed" in body or "issue content changed" in body
                for _, body in scenario.github.posted_comments
            )
        )
        # The retry produced a commit, so the issue advanced to validating and
        # the command comment is consumed (won't re-fire next tick).
        self.assertIn((CONTINUE_RETRY_ISSUE, LABEL_VALIDATING), scenario.github.label_history)
        self.assertEqual(len(scenario.github.opened_prs), 1)
        self.assertEqual(
            scenario.github.pinned_data(CONTINUE_RETRY_ISSUE).get(LAST_ACTION_COMMENT_ID),
            COMMAND_COMMENT_ID,
        )

    def test_question_park_bare_continue_refuses(
        self,
    ) -> None:
        # A real agent question parks with `park_reason=None`. A content-free
        # continue carries no answer, so refuse and stay parked -- and the
        # refusal must not re-post every tick.
        scenario = IssueScenario(
            *_seed_parked_implementing(
                CONTINUE_QUESTION_ISSUE,
                park_reason=None,
                drift_neutral=True,
            )
        )

        mocks = self._run_implementing(
            scenario.github,
            scenario.issue,
            run_agent=_agent(),
        )
        # Second tick with no new human comment must not re-refuse or resume.
        self._run_implementing(
            scenario.github,
            scenario.issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        refusals = [body for _, body in scenario.github.posted_comments if "needs your actual guidance" in body]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(scenario.github.opened_prs, [])
        state = scenario.github.pinned_data(CONTINUE_QUESTION_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # Command AND the refusal are consumed so nothing re-fires.
        self.assertGreaterEqual(
            int(state.get(LAST_ACTION_COMMENT_ID)),
            COMMAND_COMMENT_ID,
        )

    def test_guided_continue_keeps_guidance(self) -> None:
        # A `/orchestrator continue` posted ALONGSIDE real guidance is not a
        # bare command: it falls through to the normal drift resume, which
        # feeds the guidance to the dev (it must not be dropped).
        gh, issue = _seed_parked_implementing(
            CONTINUE_GUIDED_ISSUE,
            park_reason="agent_silent",
            command_body=f"{CONTINUE_COMMAND}\nrename the flag to --strict",
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="done"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["sha-before", "sha-after"],
        )

        mocks[RUN_AGENT].assert_called_once()
        prompt = mocks[RUN_AGENT].call_args[0][1]
        self.assertIn("rename the flag to --strict", prompt)
