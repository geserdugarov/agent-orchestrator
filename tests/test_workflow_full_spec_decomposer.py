# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for full-spec decomposer persistence behavior."""

from __future__ import annotations

import unittest

from tests import implementing_full_spec_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
CLAUDE_ARGS = support.CLAUDE_ARGS
CLAUDE_SPEC = support.CLAUDE_SPEC
CODEX_ARGS = support.CODEX_ARGS
CODEX_SPEC = support.CODEX_SPEC
DECOMPOSER_AGENT = support.DECOMPOSER_AGENT
DECOMPOSER_PARK_ID = support.DECOMPOSER_PARK_ID
DECOMPOSER_REPLY_ID = support.DECOMPOSER_REPLY_ID
EXTRA_ARGS = support.EXTRA_ARGS
FRESH_DECOMPOSER_ISSUE = support.FRESH_DECOMPOSER_ISSUE
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakeUser = support.FakeUser
LABEL_DECOMPOSING = support.LABEL_DECOMPOSING
RESUMED_DECOMPOSER_ISSUE = support.RESUMED_DECOMPOSER_ISSUE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
RUN_AGENT = support.RUN_AGENT
TEST_AUTHOR = support.TEST_AUTHOR
_FullSpecFixtureMixin = support._FullSpecFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
make_issue = support.make_issue
workflow = support.workflow


class FullSpecDecomposerPersistenceTest(
    unittest.TestCase,
    _FullSpecFixtureMixin,
):
    def test_fresh_decomposer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        self._enter(
            self._patch_decompose_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )

        # Manifest "single" -- simplest successful decompose path.
        manifest = 'OK\n\n```orchestrator-manifest\n{"decision": "single", "rationale": "fits one context"}\n```\n'
        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-67020", last_message=manifest),
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        self._pinned_data = gh.pinned_data(FRESH_DECOMPOSER_ISSUE)
        # Full spec, not the bare backend.
        self.assertEqual(self._pinned_data[DECOMPOSER_AGENT], CODEX_SPEC)
        self.assertEqual(self._pinned_data["decomposer_session_id"], "dec-67020")

    def test_decomposer_resume_uses_stored_spec(self) -> None:
        # Pinned with the full codex spec. After DECOMPOSE_AGENT flips to
        # claude, the awaiting-human resume must still resume on codex
        # with the codex args, not retarget the next call to claude.
        gh = FakeGitHubClient()
        issue = make_issue(
            RESUMED_DECOMPOSER_ISSUE,
            label=LABEL_DECOMPOSING,
            comments=[
                FakeComment(id=DECOMPOSER_PARK_ID, body="park", user=FakeUser("orchestrator")),
                FakeComment(id=DECOMPOSER_REPLY_ID, body="please split", user=FakeUser(TEST_AUTHOR)),
            ],
        )
        gh.add_issue(issue)
        gh.seed_state(
            RESUMED_DECOMPOSER_ISSUE,
            awaiting_human=True,
            last_action_comment_id=DECOMPOSER_PARK_ID,
            decomposer_agent=CODEX_SPEC,
            decomposer_session_id="dec-67021",
        )

        self._enter(
            self._patch_decompose_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67021",
                last_message=('OK\n\n```orchestrator-manifest\n{"decision": "single", "rationale": "ok"}\n```\n'),
            ),
        )

        # Resume call used the stored backend AND the stored args.
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(RESUME_SESSION_ID), "dec-67021")
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # Stored spec untouched.
        self.assertEqual(
            gh.pinned_data(RESUMED_DECOMPOSER_ISSUE).get(DECOMPOSER_AGENT),
            CODEX_SPEC,
        )

    def test_legacy_bare_decomposer_still_works(self) -> None:
        # `decomposer_agent="codex"` (no args) is the legacy pinned form;
        # it must continue to round-trip cleanly to `("codex", "codex", ())`.
        spec, backend, args, sid = workflow._read_decomposer_session(
            workflow.PinnedState(
                data={DECOMPOSER_AGENT: BACKEND_CODEX, "decomposer_session_id": "sid-x"},
            )
        )
        self.assertEqual(spec, BACKEND_CODEX)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, ())
        self.assertEqual(sid, "sid-x")
