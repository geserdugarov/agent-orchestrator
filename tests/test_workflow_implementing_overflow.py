# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing overflow behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

IssueScenario = support.IssueScenario

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
DEFAULT_SESSION = support.DEFAULT_SESSION
DONE_MESSAGE = support.DONE_MESSAGE
ENSURE_WORKTREE = support.ENSURE_WORKTREE
FRESH_SESSION = support.FRESH_SESSION
KEY_DEV_SESSION_ID = support.KEY_DEV_SESSION_ID
KEY_SILENT_PARK_COUNT = support.KEY_SILENT_PARK_COUNT
MagicMock = support.MagicMock
OK_MESSAGE = support.OK_MESSAGE
POISONED_SESSION = support.POISONED_SESSION
PROMPT_TOO_LONG_MESSAGE = support.PROMPT_TOO_LONG_MESSAGE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RESUME_TEXT = support.RESUME_TEXT
RUN_AGENT = support.RUN_AGENT
_FAKE_WT = support._FAKE_WT
_OverflowSessionFixtureMixin = support._OverflowSessionFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
patch = support.patch
workflow = support.workflow


class ContextOverflowClassifierTest(
    unittest.TestCase,
    _OverflowSessionFixtureMixin,
):
    """A claude `--resume` whose replayed transcript outgrew the model context
    window comes back with "Prompt is too long" and does no work. Resuming the
    same session only re-fails (every human "continue" / "decompose and
    continue" reply just appends to an already-over-budget transcript), so it
    is treated as a poisoned session: drop the id and retry once as a fresh
    spawn in the same worktree, exactly like the stale-session path.
    """

    def test_detector_matches_known_phrasings(self) -> None:
        # The detector keys off a lowercase PREFIX of the last agent message
        # so the bare phrase, a token-count suffix, and mixed casing all trip
        # recovery.
        for last_message in (
            PROMPT_TOO_LONG_MESSAGE,
            "prompt is too long: 215000 tokens > 200000 maximum",
            "PROMPT IS TOO LONG",
            "Input is too long",
            "input length and `max_tokens` exceed context limit: ...",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id="", last_message=last_message)
                self.assertTrue(
                    workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result),
                    f"{last_message!r} should be classified context overflow",
                )

    def test_overflow_detector_matches_stderr(self) -> None:
        # The CLI may print the diagnostic to stderr without emitting a result
        # event (empty last_message); a substring match still trips recovery.
        agent_result = _agent(
            session_id="",
            last_message="",
            stderr="API Error: prompt is too long: 210000 tokens > 200000",
        )
        self.assertTrue(workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result))

    def test_detector_ignores_midanswer_phrase(self) -> None:
        # An agent that merely MENTIONS the phrase inside a normal answer must
        # not be misclassified -- last_message is matched as a prefix only.
        agent_result = _agent(
            session_id=DEFAULT_SESSION,
            last_message="I split the work because the prompt is too long to handle in one pass; see the sub-issues.",
        )
        self.assertFalse(workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result))

    def test_overflow_detector_ignores_unrelated(self) -> None:
        agent_result = _agent(
            session_id=DEFAULT_SESSION,
            last_message=DONE_MESSAGE,
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result))

    def test_detector_only_triggers_for_claude(self) -> None:
        agent_result = _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE)
        self.assertFalse(workflow._is_context_overflow_failure(BACKEND_CODEX, agent_result))

    def test_poisoned_covers_stale_and_overflow(self) -> None:
        stale = _agent(
            session_id="",
            last_message="",
            stderr="No conversation found with session ID: x",
        )
        overflow = _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE)
        unrelated = _agent(session_id=DEFAULT_SESSION, last_message="a question?")
        self.assertTrue(workflow._is_poisoned_session_failure(BACKEND_CLAUDE, stale))
        self.assertTrue(workflow._is_poisoned_session_failure(BACKEND_CLAUDE, overflow))
        self.assertFalse(workflow._is_poisoned_session_failure(BACKEND_CLAUDE, unrelated))


class ContextOverflowImmediateRetryTest(
    unittest.TestCase,
    _OverflowSessionFixtureMixin,
):
    def test_claude_overflow_retries_fresh(self) -> None:
        # First call resumes the poisoned (overflowed) session and returns the
        # marker; second is a fresh spawn (no resume id) whose new session id
        # is persisted on success.
        scenario = IssueScenario(*self._seeded_issue())
        state = scenario.github.read_pinned_state(scenario.issue)

        run_agent = MagicMock(
            side_effect=[
                _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE),
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
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_overflow_retry_clears_pinned(self) -> None:
        # If the fresh-spawn retry returns no session id, the poisoned id must
        # still be cleared so the next tick's `_read_dev_session` cannot
        # resurrect the overflowed session.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(
            side_effect=[
                _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE),
                _agent(session_id="", last_message=""),
            ]
        )

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertIsNone(state.get(KEY_DEV_SESSION_ID))

    def test_repeated_overflow_does_not_loop(self) -> None:
        # A fresh spawn that ALSO overflows (issue body so large even a small
        # prompt exceeds the window) is bounded to a single retry; the still-
        # failing result is surfaced so the caller's `_on_question` parks it
        # for human intervention (split the issue) rather than looping.
        scenario = IssueScenario(*self._seeded_issue())
        self._state = scenario.github.read_pinned_state(scenario.issue)

        overflow_result = _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE)
        run_agent = MagicMock(side_effect=[overflow_result, overflow_result])

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
        self.assertEqual(agent_result.last_message, PROMPT_TOO_LONG_MESSAGE)

    def test_codex_overflow_no_immediate_retry(self) -> None:
        # Codex has no analogous stable marker; a codex resume whose message
        # happens to share the text must not trip the claude-only retry.
        gh, issue = self._seeded_issue(dev_agent=BACKEND_CODEX)
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(return_value=_agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE))

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
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)
