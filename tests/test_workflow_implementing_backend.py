# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing backend behavior."""

from __future__ import annotations

import unittest

from tests import implementing_retry_test_support as support

ACTION_COMMENT_ID = support.ACTION_COMMENT_ID
BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
DEV_FIX_BACKEND_ISSUE = support.DEV_FIX_BACKEND_ISSUE
DEV_SESSION = support.DEV_SESSION
DONE_MESSAGE = support.DONE_MESSAGE
FRESH_BACKEND_ISSUE = support.FRESH_BACKEND_ISSUE
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakeUser = support.FakeUser
HUMAN_REPLY_ID = support.HUMAN_REPLY_ID
KEY_CODEX_SESSION_ID = support.KEY_CODEX_SESSION_ID
KEY_DEV_AGENT = support.KEY_DEV_AGENT
KEY_DEV_SESSION_ID = support.KEY_DEV_SESSION_ID
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_VALIDATING = support.LABEL_VALIDATING
LEGACY_BACKEND_ISSUE = support.LEGACY_BACKEND_ISSUE
LEGACY_SESSION = support.LEGACY_SESSION
OK_MESSAGE = support.OK_MESSAGE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
REVIEW_APPROVED_MESSAGE = support.REVIEW_APPROVED_MESSAGE
REVIEW_BACKEND_ISSUE = support.REVIEW_BACKEND_ISSUE
RUN_AGENT = support.RUN_AGENT
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
_issue_branch = support._issue_branch
config = support.config
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow


class ConfigurableBackendTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The dev/review backends are picked from config, with the dev backend
    locked to whatever wrote `dev_session_id` (or legacy `codex_session_id`)
    so a config flip mid-flight does not break a resumable session.
    """

    def test_fresh_spawn_uses_dev_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_BACKEND_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", BACKEND_CLAUDE):
            mocks = self._run_implementing(
                gh,
                issue,
                run_agent=_agent(session_id="sess-fresh", last_message=DONE_MESSAGE),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CLAUDE)
        pinned_data = gh.pinned_data(FRESH_BACKEND_ISSUE)
        self.assertEqual(pinned_data[KEY_DEV_AGENT], BACKEND_CLAUDE)
        self.assertEqual(pinned_data[KEY_DEV_SESSION_ID], "sess-fresh")
        self.assertNotIn(KEY_CODEX_SESSION_ID, pinned_data)

    def test_reviewer_spawn_uses_review_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(REVIEW_BACKEND_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            REVIEW_BACKEND_ISSUE,
            pr_number=REVIEW_BACKEND_ISSUE,
            branch=_issue_branch(REVIEW_BACKEND_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", BACKEND_CODEX):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message=REVIEW_APPROVED_MESSAGE,
                ),
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        pinned_data = gh.pinned_data(REVIEW_BACKEND_ISSUE)
        self.assertEqual(pinned_data["review_agent"], BACKEND_CODEX)
        self.assertEqual(pinned_data["last_review_session_id"], "rev-sess")

    def test_dev_fix_uses_recorded_backend_not_config(self) -> None:
        # Issue locked to codex via pinned state; even if config flips to
        # claude, the validating dev-fix call must stay on codex.
        gh = FakeGitHubClient()
        issue = make_issue(DEV_FIX_BACKEND_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            DEV_FIX_BACKEND_ISSUE,
            pr_number=DEV_FIX_BACKEND_ISSUE,
            branch=_issue_branch(DEV_FIX_BACKEND_ISSUE),
            dev_agent=BACKEND_CODEX,
            dev_session_id=DEV_SESSION,
            review_round=0,
        )
        with (
            patch.object(config, "DEV_AGENT", BACKEND_CLAUDE),
            patch.object(config, "REVIEW_AGENT", BACKEND_CLAUDE),
        ):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[
                    _agent(
                        session_id="rev-sess",
                        last_message=("1. Tighten\n\nVERDICT: CHANGES_REQUESTED"),
                    ),
                    _agent(session_id=DEV_SESSION, last_message="fixed"),
                ],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "aaa", "bbb"],
            )

        # Reviewer takes config; dev-fix takes pinned state.
        self.assertEqual(mocks[RUN_AGENT].call_count, 2)
        agent_calls = mocks[RUN_AGENT].call_args_list
        self.assertEqual(agent_calls[0].args[0], BACKEND_CLAUDE)
        self.assertEqual(agent_calls[1].args[0], BACKEND_CODEX)
        self.assertEqual(
            agent_calls[1].kwargs.get(RESUME_SESSION_ID),
            DEV_SESSION,
        )

    def test_legacy_codex_session_resumes(self) -> None:
        # Pinned state predates the rollout: only `codex_session_id`. Resume
        # on human reply must stick with codex even when DEV_AGENT=claude.
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_BACKEND_ISSUE, label=LABEL_IMPLEMENTING)
        reply = FakeComment(
            id=HUMAN_REPLY_ID,
            body="use sqlite",
            user=FakeUser("alice"),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_BACKEND_ISSUE,
            awaiting_human=True,
            last_action_comment_id=ACTION_COMMENT_ID,
            codex_session_id=LEGACY_SESSION,
            branch=_issue_branch(LEGACY_BACKEND_ISSUE),
        )

        with patch.object(config, "DEV_AGENT", BACKEND_CLAUDE):
            mocks = self._run_implementing(
                gh,
                issue,
                run_agent=_agent(session_id=LEGACY_SESSION, last_message=OK_MESSAGE),
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs.get(RESUME_SESSION_ID),
            LEGACY_SESSION,
        )
        # No proactive migration: legacy key stays put, no new keys written
        # by a resume (only fresh spawns write `dev_agent`/`dev_session_id`).
        pinned_data = gh.pinned_data(LEGACY_BACKEND_ISSUE)
        self.assertEqual(pinned_data.get(KEY_CODEX_SESSION_ID), LEGACY_SESSION)
        self.assertNotIn(KEY_DEV_AGENT, pinned_data)
        self.assertNotIn(KEY_DEV_SESSION_ID, pinned_data)
