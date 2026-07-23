# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing stale session behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

IssueScenario = support.IssueScenario

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
ENSURE_WORKTREE = support.ENSURE_WORKTREE
FRESH_SESSION = support.FRESH_SESSION
KEY_CODEX_SESSION_ID = support.KEY_CODEX_SESSION_ID
KEY_DEV_AGENT = support.KEY_DEV_AGENT
KEY_DEV_SESSION_ID = support.KEY_DEV_SESSION_ID
KEY_SILENT_PARK_COUNT = support.KEY_SILENT_PARK_COUNT
MagicMock = support.MagicMock
OK_MESSAGE = support.OK_MESSAGE
POISONED_SESSION = support.POISONED_SESSION
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RESUME_TEXT = support.RESUME_TEXT
RUN_AGENT = support.RUN_AGENT
STALE_SESSION_STDERR = support.STALE_SESSION_STDERR
_FAKE_WT = support._FAKE_WT
_StaleSessionFixtureMixin = support._StaleSessionFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
patch = support.patch
workflow = support.workflow


class StaleSessionClassifierTest(unittest.TestCase, _StaleSessionFixtureMixin):
    """When Claude's `--resume <sid>` lands on a transcript that no longer
    exists, the CLI prints `No conversation found with session ID` on stderr
    and exits with empty stdout. Without an immediate retry, the resume
    would park `agent_silent` and the `_SILENT_PARKS_BEFORE_FRESH_SESSION`
    threshold path would wait for a second silent park before recovering.
    `_resume_dev_with_text` short-circuits that by detecting the marker and
    retrying once with a cleared session id in the same worktree.
    """

    def test_marker_detector_matches_known_phrasings(self) -> None:
        # The detector is keyed off lowercase substrings so phrasing tweaks
        # across Claude CLI releases still trip the recovery path.
        for stderr in (
            "Error: No conversation found with session ID: abc-123",
            "no conversation found with id abc",
            "No conversation with session ID xyz",
            "Conversation not found.",
            # Mixed casing still matches.
            "NO CONVERSATION FOUND WITH SESSION ID foo",
        ):
            with self.subTest(stderr=stderr):
                agent_result = _agent(session_id="", last_message="", stderr=stderr)
                self.assertTrue(
                    workflow._is_stale_session_failure(BACKEND_CLAUDE, agent_result),
                    f"{stderr!r} should be classified stale-session",
                )

    def test_marker_detector_ignores_unrelated_stderr(self) -> None:
        agent_result = _agent(
            session_id="",
            last_message="",
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(workflow._is_stale_session_failure(BACKEND_CLAUDE, agent_result))

    def test_marker_detector_only_triggers_for_claude(self) -> None:
        # Codex has no analogous stable marker today; the detector must
        # not misfire on a codex resume whose stderr happens to share text.
        agent_result = _agent(
            session_id="",
            last_message="",
            stderr="No conversation found with session ID: xyz",
        )
        self.assertFalse(workflow._is_stale_session_failure(BACKEND_CODEX, agent_result))


class StaleSessionImmediateRetryTest(
    unittest.TestCase,
    _StaleSessionFixtureMixin,
):
    def test_claude_stale_session_retries_fresh(self) -> None:
        # Two calls expected: the first one resumes the poisoned session and
        # comes back with the marker; the second is a fresh spawn (no resume
        # session id) in the same worktree, with the new session id
        # persisted on success.
        scenario = IssueScenario(*self._seeded_issue())
        state = scenario.github.read_pinned_state(scenario.issue)

        stale_result = _agent(
            session_id="",
            last_message="",
            stderr=STALE_SESSION_STDERR,
        )
        run_agent = MagicMock(
            side_effect=[
                stale_result,
                _agent(session_id=FRESH_SESSION, last_message=OK_MESSAGE),
            ]
        )

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(scenario.github, _TEST_SPEC, scenario.issue, state, RESUME_TEXT)

        resume_ids = [agent_call.kwargs.get(RESUME_SESSION_ID) for agent_call in run_agent.call_args_list]
        self.assertEqual(
            resume_ids,
            [POISONED_SESSION, None],
            "expected one resume with the poisoned id then one fresh spawn",
        )
        self.assertEqual(
            state.get(KEY_DEV_SESSION_ID),
            FRESH_SESSION,
            "fresh spawn's session id must be persisted",
        )
        self.assertEqual(state.get(KEY_DEV_AGENT), BACKEND_CLAUDE)
        self.assertIsNone(state.get(KEY_CODEX_SESSION_ID))
        # Silent-park streak resets so a future blip does not immediately
        # re-drop the new session.
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_stale_retry_clears_pinned(self) -> None:
        # If the fresh-spawn retry returns no session id (CLI hiccup), the
        # poisoned id must still be cleared from pinned state -- otherwise
        # the next tick's `_read_dev_session` resurrects it.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(
            side_effect=[
                _agent(session_id="", last_message="", stderr=STALE_SESSION_STDERR),
                _agent(session_id="", last_message=""),
            ]
        )

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertIsNone(
            state.get(KEY_DEV_SESSION_ID),
            "poisoned session id must be cleared even when the retry returns no session id",
        )

    def test_stale_retry_does_not_loop(self) -> None:
        # If the fresh spawn ALSO trips a stale-session marker something
        # deeper is broken (e.g. a misconfigured CLI). Surface that result
        # to the caller instead of looping infinitely.
        scenario = IssueScenario(*self._seeded_issue())
        self._state = scenario.github.read_pinned_state(scenario.issue)

        stale_result = _agent(
            session_id="",
            last_message="",
            stderr=STALE_SESSION_STDERR,
        )
        run_agent = MagicMock(side_effect=[stale_result, stale_result])

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            _, agent_result, _ = workflow._resume_dev_with_text(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
                self._state,
                RESUME_TEXT,
            )

        resume_ids = [agent_call.kwargs.get(RESUME_SESSION_ID) for agent_call in run_agent.call_args_list]
        self.assertEqual(
            resume_ids,
            [POISONED_SESSION, None],
            "retry must be bounded to a single fresh spawn",
        )
        # Result reflects the still-failing retry; caller's downstream
        # `_on_question` will handle the agent_silent park.
        self.assertEqual(agent_result.stderr, STALE_SESSION_STDERR)

    def test_codex_stale_stderr_no_immediate_retry(self) -> None:
        # Codex falls back to the silent-park-count path. A first resume
        # whose stderr happens to contain the marker must NOT retry
        # immediately for the codex backend.
        gh, issue = self._seeded_issue(dev_agent=BACKEND_CODEX)
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(
            return_value=_agent(
                session_id="",
                last_message="",
                stderr=STALE_SESSION_STDERR,
            )
        )

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertEqual(
            [run_agent.call_args.kwargs.get(RESUME_SESSION_ID)],
            [POISONED_SESSION],
            "codex backend must NOT trigger the claude-only immediate retry",
        )
        # Poisoned id remains; the existing silent-park-count path is what
        # will eventually drop it.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)
