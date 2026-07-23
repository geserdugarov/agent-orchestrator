# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing rotation behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
DONE_MESSAGE = support.DONE_MESSAGE
EMPTY_SESSION_RESULT_ISSUE = support.EMPTY_SESSION_RESULT_ISSUE
FIX_PROMPT_FRAGMENT = support.FIX_PROMPT_FRAGMENT
FRESH_SESSION = support.FRESH_SESSION
FakeGitHubClient = support.FakeGitHubClient
HIGH_RESUME_COUNT = support.HIGH_RESUME_COUNT
IMPLEMENT_PROMPT_FRAGMENT = support.IMPLEMENT_PROMPT_FRAGMENT
KEY_DEV_RESUME_COUNT = support.KEY_DEV_RESUME_COUNT
KEY_DEV_SESSION_ID = support.KEY_DEV_SESSION_ID
LABEL_DOCUMENTING = support.LABEL_DOCUMENTING
LIVE_SESSION = support.LIVE_SESSION
MISSING_SESSION_ISSUE = support.MISSING_SESSION_ISSUE
MagicMock = support.MagicMock
OK_MESSAGE = support.OK_MESSAGE
POISONED_SESSION = support.POISONED_SESSION
PREAMBLE_ISSUE = support.PREAMBLE_ISSUE
PROMPT_TOO_LONG_MESSAGE = support.PROMPT_TOO_LONG_MESSAGE
RESUME_PROMPT_FRAGMENT = support.RESUME_PROMPT_FRAGMENT
RESUME_SESSION_ID = support.RESUME_SESSION_ID
_ProactiveSessionFixtureMixin = support._ProactiveSessionFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
make_issue = support.make_issue
workflow = support.workflow


class ProactiveSessionRotationTest(
    unittest.TestCase,
    _ProactiveSessionFixtureMixin,
):
    """`--resume` replays the whole transcript every time, so a session resumed
    past `DEV_SESSION_MAX_RESUMES` is retired proactively and rebuilt fresh from
    durable state -- capping context creep BEFORE it overflows the window. Each
    resume charges one against the per-session `dev_resume_count`; a fresh spawn
    resets it and is re-grounded with the issue requirements + branch pointer.
    """

    def test_below_threshold_bumps_and_keeps_session(self) -> None:
        gh, issue = self._seeded_issue(resume_count=3)
        run_agent = MagicMock(return_value=_agent(session_id=LIVE_SESSION, last_message=DONE_MESSAGE))

        state, _ = self._run_resume(
            gh,
            issue,
            fake_run=run_agent,
            threshold=10,
        )

        self.assertEqual(
            run_agent.call_args.kwargs.get(RESUME_SESSION_ID),
            LIVE_SESSION,
            "below budget must resume in place",
        )
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT),
            4,
            "each resume charges one against the per-session budget",
        )
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), LIVE_SESSION)

    def test_threshold_rotates_to_fresh_spawn(self) -> None:
        gh, issue = self._seeded_issue(resume_count=10)
        run_agent = MagicMock(return_value=_agent(session_id=FRESH_SESSION, last_message=OK_MESSAGE))

        state, _ = self._run_resume(
            gh,
            issue,
            fake_run=run_agent,
            threshold=10,
        )

        self.assertEqual(
            [run_agent.call_args.kwargs.get(RESUME_SESSION_ID)],
            [None],
            "budget reached must fresh-spawn (no resume id), not resume",
        )
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), FRESH_SESSION)
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT),
            0,
            "rotation resets the budget for the new session",
        )

    def test_zero_threshold_disables_rotation(self) -> None:
        gh, issue = self._seeded_issue(resume_count=HIGH_RESUME_COUNT)
        run_agent = MagicMock(return_value=_agent(session_id=LIVE_SESSION, last_message=DONE_MESSAGE))

        state, _ = self._run_resume(
            gh,
            issue,
            fake_run=run_agent,
            threshold=0,
        )

        self.assertEqual(
            [run_agent.call_args.kwargs.get(RESUME_SESSION_ID)],
            [LIVE_SESSION],
            "0 = unbounded: must keep resuming regardless of count",
        )
        self.assertEqual(state.get(KEY_DEV_RESUME_COUNT), 100)

    def test_rotation_prompt_is_regrounded(self) -> None:
        # The rotated fresh spawn has no transcript, so its prompt must carry
        # the re-grounding preamble (issue body + branch pointer) AND the
        # stage followup appended after it.
        gh, issue = self._seeded_issue(resume_count=5)
        run_agent = MagicMock(return_value=_agent(session_id=FRESH_SESSION, last_message=OK_MESSAGE))

        self._run_resume(gh, issue, fake_run=run_agent, threshold=5)

        prompt = run_agent.call_args.args[1]
        self.assertIn(RESUME_PROMPT_FRAGMENT, prompt)
        self.assertIn(IMPLEMENT_PROMPT_FRAGMENT, prompt, "issue body re-grounds")
        self.assertTrue(
            prompt.rstrip().endswith(FIX_PROMPT_FRAGMENT),
            "stage followup must be appended after the preamble",
        )

    def test_resume_in_place_prompt_has_no_preamble(self) -> None:
        # A live resume already carries the issue context in its transcript, so
        # the bare followup is sent -- no re-grounding, no token duplication.
        gh, issue = self._seeded_issue(resume_count=1)
        run_agent = MagicMock(return_value=_agent(session_id=LIVE_SESSION, last_message=DONE_MESSAGE))

        self._run_resume(gh, issue, fake_run=run_agent, threshold=10)

        self.assertEqual(run_agent.call_args.args[1], FIX_PROMPT_FRAGMENT)


class ProactiveSessionRecoveryTest(
    unittest.TestCase,
    _ProactiveSessionFixtureMixin,
):
    def test_overflow_recovery_is_regrounded(self) -> None:
        # Ties the two features together: an overflowed ("Prompt is too long")
        # resume drops the session and the recovery fresh spawn -- like the
        # rotation spawn -- is re-grounded with the preamble.
        gh, issue = self._seeded_issue(resume_count=0, sid=POISONED_SESSION)
        run_agent = MagicMock(
            side_effect=[
                _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE),
                _agent(session_id=FRESH_SESSION, last_message=OK_MESSAGE),
            ]
        )

        self._run_resume(gh, issue, fake_run=run_agent, threshold=10)

        agent_calls = run_agent.call_args_list
        self.assertEqual(
            [call.kwargs.get(RESUME_SESSION_ID) for call in agent_calls],
            [POISONED_SESSION, None],
        )
        self.assertEqual(
            agent_calls[0].args[1],
            FIX_PROMPT_FRAGMENT,
            "the initial resume sends the bare followup",
        )
        self.assertIn(
            RESUME_PROMPT_FRAGMENT,
            agent_calls[1].args[1],
            "the overflow-recovery fresh spawn must be re-grounded",
        )

    def test_preamble_includes_requirements_branch(self) -> None:
        issue = make_issue(PREAMBLE_ISSUE, body="do the work", title="My Issue")
        text = workflow._build_fresh_respawn_preamble(_TEST_SPEC, issue, "@alice: please add tests", [_TEST_SPEC])
        self.assertIn("do the work", text)
        self.assertIn("@alice: please add tests", text)
        self.assertIn("git diff", text, "must point the fresh agent at the branch")
        self.assertIn("do NOT restart", text)

    def test_no_session_entry_spawns_and_persists(self) -> None:
        # `dev_agent` is pinned but `dev_session_id` is absent -- e.g. an
        # earlier backend hiccup that committed work but surfaced no session
        # id. There is nothing to resume, so the spawn must open a NEW session
        # (no resume id), re-ground it, persist the returned id, and zero the
        # stale resume count -- otherwise later resumes find no live session
        # and fresh-spawn from scratch every tick.
        gh = FakeGitHubClient()
        issue = make_issue(
            MISSING_SESSION_ISSUE,
            label=LABEL_DOCUMENTING,
            body=IMPLEMENT_PROMPT_FRAGMENT,
        )
        gh.add_issue(issue)
        gh.seed_state(
            MISSING_SESSION_ISSUE,
            dev_agent=BACKEND_CLAUDE,
            silent_park_count=0,
            dev_resume_count=7,
        )
        run_agent = MagicMock(
            return_value=_agent(
                session_id="hiccup-recovered",
                last_message=OK_MESSAGE,
            )
        )

        state, _ = self._run_resume(
            gh,
            issue,
            fake_run=run_agent,
            threshold=10,
        )

        self.assertEqual(
            [run_agent.call_args.kwargs.get(RESUME_SESSION_ID)],
            [None],
            "no live session -> fresh spawn with no resume id",
        )
        self.assertIn(
            RESUME_PROMPT_FRAGMENT,
            run_agent.call_args.args[1],
            "the fresh spawn must be re-grounded",
        )
        self.assertEqual(
            state.get(KEY_DEV_SESSION_ID),
            "hiccup-recovered",
            "the returned session id must be persisted, not dropped",
        )
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT),
            0,
            "the new session starts its resume budget from zero",
        )

    def test_missing_session_empty_result_keeps_clear(self) -> None:
        # The recovery spawn ALSO returns no session id (another hiccup): the
        # session stays unpinned so the next tick fresh-spawns again rather
        # than resuming a phantom id, and the resume budget is not charged.
        gh = FakeGitHubClient()
        issue = make_issue(
            EMPTY_SESSION_RESULT_ISSUE,
            label=LABEL_DOCUMENTING,
            body=IMPLEMENT_PROMPT_FRAGMENT,
        )
        gh.add_issue(issue)
        gh.seed_state(
            EMPTY_SESSION_RESULT_ISSUE,
            dev_agent=BACKEND_CLAUDE,
            silent_park_count=0,
            dev_resume_count=2,
        )
        run_agent = MagicMock(return_value=_agent(session_id="", last_message=""))

        state, _ = self._run_resume(
            gh,
            issue,
            fake_run=run_agent,
            threshold=10,
        )

        self.assertIsNone(
            run_agent.call_args.kwargs.get(RESUME_SESSION_ID),
            "still a fresh spawn, no resume id",
        )
        self.assertIsNone(state.get(KEY_DEV_SESSION_ID))
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT),
            2,
            "a no-session fresh spawn must not charge the resume budget",
        )
