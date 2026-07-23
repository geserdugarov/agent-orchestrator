# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing silent session behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
ENSURE_WORKTREE = support.ENSURE_WORKTREE
FRESH_SESSION = support.FRESH_SESSION
FakeGitHubClient = support.FakeGitHubClient
KEY_CODEX_SESSION_ID = support.KEY_CODEX_SESSION_ID
KEY_DEV_AGENT = support.KEY_DEV_AGENT
KEY_DEV_SESSION_ID = support.KEY_DEV_SESSION_ID
KEY_SILENT_PARK_COUNT = support.KEY_SILENT_PARK_COUNT
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LEGACY_FRESH_SESSION_ISSUE = support.LEGACY_FRESH_SESSION_ISSUE
MagicMock = support.MagicMock
OK_MESSAGE = support.OK_MESSAGE
POISONED_SESSION = support.POISONED_SESSION
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RESUME_TEXT = support.RESUME_TEXT
RUN_AGENT = support.RUN_AGENT
_FAKE_WT = support._FAKE_WT
_SilentSessionFixtureMixin = support._SilentSessionFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow


class SilentSessionResumeFallbackTest(
    unittest.TestCase,
    _SilentSessionFixtureMixin,
):
    """`_resume_dev_with_text` drops a poisoned `dev_session_id` after
    `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent` parks
    and starts a fresh spawn instead. Without this fallback every human
    "retry" comment burns another fresh-spawn retry slot on the same dead
    session (the Claude rate-limit kill shape documented in #24).
    """

    def test_below_threshold_keeps_session(self) -> None:
        # One prior silent park is treated as a transient blip, not a
        # poisoned session: the resume still passes the original session
        # id and the streak counter stays put for the next park to bump.
        gh, issue = self._seeded_issue(silent_park_count=1)
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(return_value=_agent(session_id="ignored", last_message=OK_MESSAGE))

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertEqual(
            run_agent.call_args.kwargs.get(RESUME_SESSION_ID),
            POISONED_SESSION,
            "below threshold the original session id must still be resumed",
        )
        # Session id and streak are not touched on the below-threshold path.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 1)

    def test_threshold_drops_and_persists_session(self) -> None:
        # `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive silent parks ==
        # session is poisoned. The resume must call `run_agent` with
        # `resume_session_id=None`, persist the new session id from the
        # result, and reset the silent-park streak so the new session
        # starts with a clean budget.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(return_value=_agent(session_id=FRESH_SESSION, last_message=OK_MESSAGE))

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertIsNone(
            run_agent.call_args.kwargs.get(RESUME_SESSION_ID),
            "fresh spawn must drop the poisoned dev_session_id",
        )
        self.assertEqual(run_agent.call_args.args[0], BACKEND_CLAUDE)
        # New session id must be persisted so the next resume picks it up
        # instead of looking up an empty `dev_session_id` and re-spawning.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), FRESH_SESSION)
        # Streak resets so a future blip doesn't drop the new session
        # immediately.
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_fresh_session_clears_pinned(self) -> None:
        # If the fresh spawn comes back without a `session_id` (agent
        # backend hiccup, missing file, etc.), the poisoned id must STILL
        # be removed from pinned state. Otherwise `_read_dev_session` on
        # the next tick returns the dead session and the resume loop
        # re-poisons itself.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(
                workflow,
                RUN_AGENT,
                lambda *args, **kwargs: _agent(session_id="", last_message=""),
            ),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        self.assertIsNone(
            state.get(KEY_DEV_SESSION_ID),
            "poisoned session id must be cleared even when the fresh spawn returns no session_id",
        )

    def test_fresh_spawn_clears_legacy_session(self) -> None:
        # An issue still on the legacy `codex_session_id` schema must
        # also have that field cleared on fresh-spawn -- otherwise the
        # next tick's `_read_dev_session` falls through the new keys
        # (because `dev_session_id` is None) and resurrects the poisoned
        # legacy id.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_FRESH_SESSION_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_FRESH_SESSION_ISSUE,
            # Legacy schema: only `codex_session_id` is set, no `dev_agent`.
            codex_session_id="poisoned-legacy",
            silent_park_count=threshold,
        )
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(return_value=_agent(session_id="fresh-legacy", last_message=OK_MESSAGE))

        with (
            patch.object(workflow, ENSURE_WORKTREE, return_value=_FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, RESUME_TEXT)

        # Backend stays locked to codex (legacy).
        self.assertEqual(run_agent.call_args.args[0], BACKEND_CODEX)
        # Resume happened with no session id -- the poisoned legacy id
        # was dropped.
        self.assertIsNone(run_agent.call_args.kwargs.get(RESUME_SESSION_ID))
        # Pinned state migrated to the new keys with the fresh session
        # id, and the legacy field is cleared.
        self.assertEqual(state.get(KEY_DEV_AGENT), BACKEND_CODEX)
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), "fresh-legacy")
        self.assertIsNone(state.get(KEY_CODEX_SESSION_ID))
