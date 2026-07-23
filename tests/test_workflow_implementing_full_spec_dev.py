# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing full spec dev behavior."""

from __future__ import annotations

import unittest

from tests import implementing_full_spec_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
CLAUDE_ARGS = support.CLAUDE_ARGS
CLAUDE_SPEC = support.CLAUDE_SPEC
CODEX_ARGS = support.CODEX_ARGS
CODEX_SESSION_REPLY_ID = support.CODEX_SESSION_REPLY_ID
CODEX_SPEC = support.CODEX_SPEC
DEV_AGENT_KEY = support.DEV_AGENT_KEY
DEV_SESSION_ID = support.DEV_SESSION_ID
EXTRA_ARGS = support.EXTRA_ARGS
FRESH_DEV_ISSUE = support.FRESH_DEV_ISSUE
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakeUser = support.FakeUser
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_VALIDATING = support.LABEL_VALIDATING
LEGACY_ACTION_WATERMARK = support.LEGACY_ACTION_WATERMARK
LEGACY_BACKEND_ISSUE = support.LEGACY_BACKEND_ISSUE
LEGACY_REPLY_ID = support.LEGACY_REPLY_ID
LEGACY_SESSION_ISSUE = support.LEGACY_SESSION_ISSUE
MagicMock = support.MagicMock
OK_MESSAGE = support.OK_MESSAGE
POISONED_DROP_ISSUE = support.POISONED_DROP_ISSUE
POISONED_LEGACY_ISSUE = support.POISONED_LEGACY_ISSUE
RESUMED_DEV_ISSUE = support.RESUMED_DEV_ISSUE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
REVIEW_CHANGES_REQUESTED_MESSAGE = support.REVIEW_CHANGES_REQUESTED_MESSAGE
RUN_AGENT = support.RUN_AGENT
TEST_AUTHOR = support.TEST_AUTHOR
UNCHANGED_SHA = support.UNCHANGED_SHA
_FAKE_WT = support._FAKE_WT
_FullSpecFixtureMixin = support._FullSpecFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
_issue_branch = support._issue_branch
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow


class FullSpecDevPersistenceTest(unittest.TestCase, _FullSpecFixtureMixin):
    def test_fresh_spawn_stores_spec_with_args(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(
            self._patch_dev_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="sess-67001", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs[EXTRA_ARGS],
            CODEX_ARGS,
        )
        pinned_data = gh.pinned_data(FRESH_DEV_ISSUE)
        # Full spec verbatim, NOT just the parsed backend.
        self.assertEqual(pinned_data[DEV_AGENT_KEY], CODEX_SPEC)
        self.assertEqual(pinned_data[DEV_SESSION_ID], "sess-67001")

    def test_resume_uses_spec_after_env_flip(self) -> None:
        # Pinned state recorded codex+args. Even after a config flip to
        # plain claude, the resume MUST keep the recorded backend AND
        # the recorded args -- the new backend's CLI would reject codex
        # flags, and silently dropping them on a resume changes what
        # the agent actually sees mid-flight.
        gh = FakeGitHubClient()
        issue = make_issue(RESUMED_DEV_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            RESUMED_DEV_ISSUE,
            pr_number=RESUMED_DEV_ISSUE,
            branch=_issue_branch(RESUMED_DEV_ISSUE),
            dev_agent=CODEX_SPEC,
            dev_session_id="dev-67002",
            review_round=0,
        )
        # Config now points to plain claude (no args).
        self._enter(self._patch_dev_config(CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS))
        # Reviewer too -- we just want the dev-fix call to use the stored spec.
        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        review = _agent(
            session_id="rev-67002",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        dev_fix = _agent(session_id="dev-67002", last_message="fixed")

        self._mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=[UNCHANGED_SHA, UNCHANGED_SHA, "bbb"],
        )

        # Two calls: reviewer (claude per config) then dev-fix (codex per
        # pinned state).
        self.assertEqual(self._mocks[RUN_AGENT].call_count, 2)
        dev_call = self._mocks[RUN_AGENT].call_args_list[1]
        self.assertEqual(dev_call.args[0], BACKEND_CODEX)
        self.assertEqual(dev_call.kwargs.get(RESUME_SESSION_ID), "dev-67002")
        # Args came from the stored spec, NOT the current config.
        self.assertEqual(dev_call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)

    def test_legacy_bare_backend_still_works(self) -> None:
        # An issue pinned with the pre-#67 bare-backend value (`"codex"`)
        # must still resume on codex with no args -- that is what those
        # deployments had at the time the session was spawned.
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_BACKEND_ISSUE, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=LEGACY_REPLY_ID,
            body="please retry",
            user=FakeUser(TEST_AUTHOR),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_BACKEND_ISSUE,
            awaiting_human=True,
            last_action_comment_id=LEGACY_ACTION_WATERMARK,
            dev_agent=BACKEND_CODEX,  # legacy bare-backend pinned form.
            dev_session_id="dev-legacy-spec",
            branch=_issue_branch(LEGACY_BACKEND_ISSUE),
        )

        # Flip current config to a spec with args -- which the resume must IGNORE.
        self._enter(
            self._patch_dev_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="dev-legacy-spec", last_message=OK_MESSAGE),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        # No args -- the legacy pinned form had none.
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())
        # No proactive migration -- a resume does NOT rewrite `dev_agent`.
        self.assertEqual(gh.pinned_data(LEGACY_BACKEND_ISSUE).get(DEV_AGENT_KEY), BACKEND_CODEX)

    def test_legacy_codex_session_resumes_no_args(self) -> None:
        # The pre-rollout schema only had `codex_session_id`. Resume MUST
        # use codex regardless of any current config flip, and MUST pass
        # no args (the spec at the time was bare codex).
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_SESSION_ISSUE, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=CODEX_SESSION_REPLY_ID,
            body="retry",
            user=FakeUser(TEST_AUTHOR),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_SESSION_ISSUE,
            awaiting_human=True,
            last_action_comment_id=LEGACY_REPLY_ID,
            codex_session_id="sess-legacy-67004",
            branch=_issue_branch(LEGACY_SESSION_ISSUE),
        )

        self._enter(
            self._patch_dev_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )

        mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="sess-legacy-67004", last_message=OK_MESSAGE),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(RESUME_SESSION_ID), "sess-legacy-67004")
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())

    def test_poisoned_drop_keeps_full_spec(self) -> None:
        # After hitting the silent-park threshold, the resume drops the
        # poisoned session id and starts a fresh spawn. The stored
        # full spec MUST be preserved so the fresh spawn uses the same
        # backend+args (a poisoned session is a transcript problem,
        # not a backend-selection problem).
        gh = FakeGitHubClient()
        issue = make_issue(POISONED_DROP_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            POISONED_DROP_ISSUE,
            dev_agent=CODEX_SPEC,
            dev_session_id="poisoned-67005",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(return_value=_agent(session_id="fresh-67005", last_message=OK_MESSAGE))

        with (
            patch.object(workflow, "_ensure_worktree", lambda spec, issue_number, **_: _FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        call = run_agent.call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertIsNone(call.kwargs.get(RESUME_SESSION_ID))
        # Critical: the args from the stored spec survived the drop.
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # Stored spec is untouched -- not overwritten with the bare backend.
        self.assertEqual(state.get(DEV_AGENT_KEY), CODEX_SPEC)
        self.assertEqual(state.get(DEV_SESSION_ID), "fresh-67005")

    def test_poisoned_legacy_session_pins_codex(self) -> None:
        # Legacy schema (only `codex_session_id`): a poisoned-session drop
        # must pin `dev_agent="codex"` before clearing the legacy field,
        # so a subsequent env flip to claude cannot retroactively switch
        # the backend.
        gh = FakeGitHubClient()
        issue = make_issue(POISONED_LEGACY_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            POISONED_LEGACY_ISSUE,
            codex_session_id="poisoned-legacy-67006",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        self._enter(
            self._patch_dev_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )

        run_agent = MagicMock(
            return_value=_agent(
                session_id="fresh-legacy-67006",
                last_message=OK_MESSAGE,
            )
        )

        with (
            patch.object(workflow, "_ensure_worktree", lambda spec, issue_number, **_: _FAKE_WT),
            patch.object(workflow, RUN_AGENT, run_agent),
        ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (the legacy implicit spec).
        call = run_agent.call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())
        # Migrated to the new key with the legacy backend pinned, legacy
        # field cleared.
        self.assertEqual(state.get(DEV_AGENT_KEY), BACKEND_CODEX)
        self.assertEqual(state.get(DEV_SESSION_ID), "fresh-legacy-67006")
        self.assertIsNone(state.get("codex_session_id"))
