# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for full-spec persistence without active sessions."""

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
DECOMPOSER_ENV_FLIP_REPLY_ID = support.DECOMPOSER_ENV_FLIP_REPLY_ID
DEV_AGENT_KEY = support.DEV_AGENT_KEY
DEV_ENV_FLIP_REPLY_ID = support.DEV_ENV_FLIP_REPLY_ID
DEV_SESSION_ID = support.DEV_SESSION_ID
ENV_FLIP_DECOMPOSER_ISSUE = support.ENV_FLIP_DECOMPOSER_ISSUE
ENV_FLIP_DEV_ISSUE = support.ENV_FLIP_DEV_ISSUE
EXTRA_ARGS = support.EXTRA_ARGS
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakeUser = support.FakeUser
KEY_AWAITING_HUMAN = support.KEY_AWAITING_HUMAN
LABEL_DECOMPOSING = support.LABEL_DECOMPOSING
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
NO_SESSION_DECOMPOSER_ISSUE = support.NO_SESSION_DECOMPOSER_ISSUE
NO_SESSION_DEV_ISSUE = support.NO_SESSION_DEV_ISSUE
RUN_AGENT = support.RUN_AGENT
TEST_AUTHOR = support.TEST_AUTHOR
_FullSpecFixtureMixin = support._FullSpecFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
make_issue = support.make_issue
workflow = support.workflow


def _assert_recorded_codex_call(
    test_case,
    mocks,
    backend_message: str,
    args_message: str | None = None,
) -> None:
    call = mocks[RUN_AGENT].call_args
    test_case.assertEqual(
        call.args[0],
        BACKEND_CODEX,
        backend_message,
    )
    test_case.assertEqual(
        call.kwargs.get(EXTRA_ARGS),
        CODEX_ARGS,
        args_message,
    )


class FullSpecDevNoSessionTest(unittest.TestCase, _FullSpecFixtureMixin):
    def test_dev_spec_pinned_without_session_id(self) -> None:
        # A fresh dev spawn that produces commits but no session id (a
        # codex `-o` file the agent left empty, an unparseable claude
        # JSONL line, etc.) MUST still pin `dev_agent` to the full
        # configured spec. Without this, a subsequent `DEV_AGENT` flip
        # would silently retarget the next validating dev-fix resume
        # at a backend that never ran on this issue.
        gh = FakeGitHubClient()
        issue = make_issue(NO_SESSION_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(
            self._patch_dev_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )

        # Empty session_id: backend hiccup, but the worktree got commits.
        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        pinned_data = gh.pinned_data(NO_SESSION_DEV_ISSUE)
        self.assertEqual(
            pinned_data.get(DEV_AGENT_KEY),
            CODEX_SPEC,
            "dev_agent must be pinned to the full spec even when the spawn returns no session_id",
        )
        # session_id was empty, so the legacy field stays absent.
        self.assertNotIn(DEV_SESSION_ID, pinned_data)

    def test_dev_env_flip_resumes_recorded_spec(self) -> None:
        # Reviewer-requested scenario: spawn returns no session id but
        # commits/parks land. Operator then flips `DEV_AGENT` between
        # ticks. The next resume MUST stick with the spec that was
        # actually running, not retarget at the new config.
        gh = FakeGitHubClient()
        issue = make_issue(ENV_FLIP_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        # First tick: codex spawn, no session id, agent question -> park
        # awaiting human.
        self._enter(
            self._patch_dev_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )
        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="", last_message="need input"),
            has_new_commits=False,
        )
        self._pinned_data = gh.pinned_data(ENV_FLIP_DEV_ISSUE)
        self.assertEqual(self._pinned_data.get(DEV_AGENT_KEY), CODEX_SPEC)
        self.assertTrue(self._pinned_data.get(KEY_AWAITING_HUMAN))

        # Second tick: operator flipped `DEV_AGENT` to claude AND
        # provided new args; a human reply lands on the issue. The
        # resume MUST stick with codex+args from pinned state, NOT
        # retarget at the current claude config.
        reply = FakeComment(
            id=DEV_ENV_FLIP_REPLY_ID,
            body="ok proceed",
            user=FakeUser(TEST_AUTHOR),
        )
        issue.comments.append(reply)

        # Switch config to claude (different backend + different args).
        # `_enter` schedules cleanup; start a fresh override block.
        for config_patch in self._patch_dev_config(CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS):
            config_patch.start()
            self.addCleanup(config_patch.stop)

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="sess-67031", last_message="done"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
            # The user-content-drift branch (which fires here because the
            # human's "ok proceed" comment changes the issue hash from
            # tick 1) snapshots HEAD before and after the resume to decide
            # whether THIS resume committed, so we need two SHA values
            # (different ones, so the post-resume "did the agent commit"
            # check goes through `_on_commits`). The drift path still
            # calls `_resume_dev_with_text` with the recorded codex spec,
            # so the call-arg assertions below still hold.
            head_shas=["before-sha", "after-sha"],
        )

        _assert_recorded_codex_call(
            self,
            mocks,
            "resume must stick with the spec the first tick recorded, NOT the new DEV_AGENT after the flip",
            args_message=("stored codex args must survive across the config flip"),
        )


class FullSpecDecomposerNoSessionTest(
    unittest.TestCase,
    _FullSpecFixtureMixin,
):
    def test_decomposer_spec_pinned_no_session(self) -> None:
        # Same reviewer concern, decomposer side: a fresh decomposer
        # that emits a manifest without surfacing a session id (or
        # parks awaiting human after a question) must still pin
        # `decomposer_agent` to the full spec.
        gh = FakeGitHubClient()
        issue = make_issue(NO_SESSION_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        self._enter(
            self._patch_decompose_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )

        # No session_id, question-only output -> awaiting human park.
        # The spec must still land in pinned state so a later config
        # flip cannot retarget the awaiting-human resume.
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="please clarify"),
        )

        pinned_data = gh.pinned_data(NO_SESSION_DECOMPOSER_ISSUE)
        self.assertEqual(
            pinned_data.get(DECOMPOSER_AGENT),
            CODEX_SPEC,
            "decomposer_agent must be pinned to the full spec even when the spawn returns no session_id",
        )
        self.assertTrue(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertNotIn("decomposer_session_id", pinned_data)

    def test_decomposer_env_flip_resumes_spec(self) -> None:
        # Same config-flip scenario, decomposer side: the awaiting-
        # human resume must stick with the recorded spec.
        gh = FakeGitHubClient()
        issue = make_issue(ENV_FLIP_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        # First tick: codex decomposer, no session id, parks on a
        # clarification request.
        self._enter(
            self._patch_decompose_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="",
                last_message="please clarify scope",
            ),
        )
        self._pinned_data = gh.pinned_data(ENV_FLIP_DECOMPOSER_ISSUE)
        self.assertEqual(self._pinned_data.get(DECOMPOSER_AGENT), CODEX_SPEC)
        self.assertTrue(self._pinned_data.get(KEY_AWAITING_HUMAN))

        # Human replies; operator flips `DECOMPOSE_AGENT` to claude
        # between ticks. The resume must stick with codex+args.
        issue.comments.append(
            FakeComment(
                id=DECOMPOSER_ENV_FLIP_REPLY_ID,
                body="single is fine",
                user=FakeUser(TEST_AUTHOR),
            )
        )
        for config_patch in self._patch_decompose_config(
            CLAUDE_SPEC,
            BACKEND_CLAUDE,
            CLAUDE_ARGS,
        ):
            config_patch.start()
            self.addCleanup(config_patch.stop)

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67033",
                last_message=(
                    'OK\n\n```orchestrator-manifest\n{"decision": "single", "rationale": "fits one context"}\n```\n'
                ),
            ),
        )

        _assert_recorded_codex_call(
            self,
            mocks,
            "decomposer resume must stick with the spec the first tick "
            "recorded, NOT the new DECOMPOSE_AGENT after the flip",
        )
