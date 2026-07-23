# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question-agent tracked-repository prompt tests."""
from __future__ import annotations

import unittest

from tests import workflow_tracked_repos_test_support as support


class QuestionSpawnTrackedReposTest(
    unittest.TestCase, support._PatchedWorkflowMixin
):
    """Question-stage prompt routing: the fresh spawn AND the no-session-id
    recovery spawn carry the block in a multi-repo deployment, while a true
    live-session resume sends the block-free followup. The block never softens
    the read-only contract."""

    def test_fresh_spawn_carries_block(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._FRESH_QUESTION_ISSUE_NUMBER,
            label=support._QUESTION_LABEL,
            body="Where does X live?",
        )
        gh.add_issue(issue)
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_question(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        # Still the question prompt with its read-only contract intact.
        self.assertIn("answering a standing question", prompt)
        self.assertIn("You MUST NOT modify", prompt)

    def test_fresh_spawn_single_repo_has_no_block(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._FRESH_QUESTION_ISSUE_NUMBER,
            label=support._QUESTION_LABEL,
            body="Where does X live?",
        )
        gh.add_issue(issue)
        with support.patch.object(support.config, support._EXPOSE_REPOS_ATTR, True), \
             support.patch.object(support.config, support._DEFAULT_SPECS_ATTR, lambda: [support._TEST_SPEC]):
            mocks = self._run(
                lambda: support.workflow._handle_question(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        self.assertNotIn(support._BLOCK_MARKER, support._prompt_of(mocks[support._RUN_AGENT_ATTR]))

    def test_no_session_recovery_has_block(self) -> None:
        # No `question_session_id` -> a transcript-less FRESH spawn. The
        # handler must send the full question prompt (block included) so the
        # recovery run sees the same context a first-tick spawn would, rather
        # than the bare followup a live session would get.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._RECOVERY_QUESTION_ISSUE_NUMBER,
            label=support._QUESTION_LABEL,
            title="Where does X live?",
            body="We need to know where X lives.",
        )
        issue.comments.append(
            support.FakeComment(
                id=support._RECOVERY_COMMENT_ID,
                body="any progress?",
                user=support.FakeUser("alice"),
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            support._RECOVERY_QUESTION_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=support._RECOVERY_WATERMARK,
            question_agent=support.config.DECOMPOSE_AGENT_SPEC,
            # No prior session id -- the previous run hiccupped.
            park_reason="question_answer",
        )
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_question(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="q-fresh", last_message="X lives in src/x.py.",
                ),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        # Fresh spawn (no resume) carrying the full question prompt + block.
        self.assertIsNone(
            mocks[support._RUN_AGENT_ATTR].call_args.kwargs.get("resume_session_id")
        )
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        self.assertIn("answering a standing question", prompt)

    def test_live_resume_followup_omits_block(self) -> None:
        # A live `question_session_id` resumes in place: the followup prompt
        # carries only the human's reply, never the block (the session already
        # saw the initial block at spawn).
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._RESUMED_QUESTION_ISSUE_NUMBER,
            label=support._QUESTION_LABEL,
            title="Q",
            body="body",
        )
        issue.comments.append(
            support.FakeComment(
                id=support._RESUME_COMMENT_ID,
                body="here is more detail",
                user=support.FakeUser("alice"),
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            support._RESUMED_QUESTION_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=support._RESUME_WATERMARK,
            question_agent=support.config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-live",
            park_reason="question_answer",
        )
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_question(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(session_id="q-live", last_message="answer"),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        self.assertNotIn(support._BLOCK_MARKER, prompt)
        # It IS the followup prompt carrying the human's reply.
        self.assertIn("here is more detail", prompt)
