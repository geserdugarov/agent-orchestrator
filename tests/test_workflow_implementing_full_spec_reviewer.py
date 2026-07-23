# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing full spec reviewer behavior."""

from __future__ import annotations

import unittest

from tests import implementing_full_spec_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
CHANGE_REQUEST_ISSUE = support.CHANGE_REQUEST_ISSUE
CODEX_ARGS = support.CODEX_ARGS
CODEX_SPEC = support.CODEX_SPEC
EXTRA_ARGS = support.EXTRA_ARGS
FRESH_REVIEW_ISSUE = support.FRESH_REVIEW_ISSUE
FakeGitHubClient = support.FakeGitHubClient
LABEL_VALIDATING = support.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = support.REVIEW_APPROVED_MESSAGE
REVIEW_CHANGES_REQUESTED_MESSAGE = support.REVIEW_CHANGES_REQUESTED_MESSAGE
REVIEW_COMMENT_ISSUE = support.REVIEW_COMMENT_ISSUE
REVIEW_PROMPT_ISSUE = support.REVIEW_PROMPT_ISSUE
RUN_AGENT = support.RUN_AGENT
UNCHANGED_SHA = support.UNCHANGED_SHA
_FullSpecFixtureMixin = support._FullSpecFixtureMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
_issue_branch = support._issue_branch
make_issue = support.make_issue
workflow = support.workflow


class FullSpecReviewerPersistenceTest(
    unittest.TestCase,
    _FullSpecFixtureMixin,
):
    def test_fresh_reviewer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_REVIEW_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            FRESH_REVIEW_ISSUE,
            pr_number=FRESH_REVIEW_ISSUE,
            branch=_issue_branch(FRESH_REVIEW_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id="dev-67010",
            review_round=0,
        )

        self._enter(
            self._patch_review_config(
                CODEX_SPEC,
                BACKEND_CODEX,
                CODEX_ARGS,
            )
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67010",
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
        )

        # Reviewer ran with backend+args from current config.
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # And the FULL spec is what gets persisted -- not just the backend.
        pinned_data = gh.pinned_data(FRESH_REVIEW_ISSUE)
        self.assertEqual(pinned_data["review_agent"], CODEX_SPEC)
        self.assertEqual(pinned_data["last_review_session_id"], "rev-67010")

    def test_reviewer_comments_use_config_backend(self) -> None:
        # Issue #67: reviewer trace/comments must not hardcode `codex` --
        # when the operator configures claude as the reviewer, the PR
        # comments must say so. We test both approval and CHANGES_REQUESTED
        # paths since both posted hardcoded text before the fix.
        gh = FakeGitHubClient()
        issue = make_issue(REVIEW_COMMENT_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            REVIEW_COMMENT_ISSUE,
            pr_number=REVIEW_COMMENT_ISSUE,
            branch=_issue_branch(REVIEW_COMMENT_ISSUE),
            dev_agent=BACKEND_CODEX,
            dev_session_id="dev-67011",
            review_round=0,
        )

        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67011",
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
        )

        approval_comments = [body for (_, body) in gh.posted_pr_comments if "review approved" in body]
        self.assertEqual(len(approval_comments), 1, approval_comments)
        self.assertIn("claude review approved", approval_comments[0])
        self.assertNotIn("codex review approved", approval_comments[0])

    def test_change_request_uses_config_backend(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(CHANGE_REQUEST_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            CHANGE_REQUEST_ISSUE,
            pr_number=CHANGE_REQUEST_ISSUE,
            branch=_issue_branch(CHANGE_REQUEST_ISSUE),
            dev_agent=BACKEND_CODEX,
            dev_session_id="dev-67012",
            review_round=0,
        )

        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        self._review = _agent(
            session_id="rev-67012",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        self._dev_fix = _agent(session_id="dev-67012", last_message="fixed")

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[self._review, self._dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=[UNCHANGED_SHA, UNCHANGED_SHA, "bbb"],
        )

        bodies = [body for (_, body) in gh.posted_pr_comments]
        review_bodies = [body for body in bodies if "review (round" in body]
        self.assertEqual(len(review_bodies), 1, review_bodies)
        self.assertIn("claude review (round", review_bodies[0])
        self.assertNotIn("codex review (round", review_bodies[0])

    def test_review_prompt_names_dev_backend(self) -> None:
        # The reviewer prompt's intro line described the implementer as
        # "a separate codex session" before the fix, which is wrong when
        # claude is the dev backend. Build the prompt directly and
        # assert it reflects the dev backend.
        prompt = workflow._build_review_prompt(
            _TEST_SPEC,
            make_issue(REVIEW_PROMPT_ISSUE),
            "",
            [_TEST_SPEC],
            dev_backend=BACKEND_CLAUDE,
        )
        self.assertIn("A separate claude session", prompt)
        self.assertNotIn("A separate codex session", prompt)
