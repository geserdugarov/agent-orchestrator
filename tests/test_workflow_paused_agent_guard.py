# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard at the non-implementing direct `_run_agent_tracked`
sites: the decomposer run in `decomposing`, the reviewer run in `validating`,
and the question run in `question`. An operator who applies `paused` (or
`backlog`) WHILE one of those agents is in flight freezes the issue before the
run's disposition -- usage counters, timeout/answer parks, child creation,
workflow relabeling, and pinned-state writes -- takes effect. Each handler
re-fetches the issue after the run returns (`gh.get_issue`) rather than trusting
the pre-run label snapshot, and on a hit returns without touching durable GitHub
state, so the next tick simply re-runs from durable state once the label is
removed.

The same guard (`_paused_during_agent_run`) that the implementing dev run uses;
these cases establish the pattern for future direct `_run_agent_tracked` paths.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _manifest,
)


def _paused_view(number: int, label: str) -> object:
    """A stage issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run."""
    view = make_issue(number, label=label)
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class DecomposerLivePauseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_during_run_blocks_children_and_relabel(self) -> None:
        # A split manifest that WOULD create two children and relabel the parent
        # to `blocked` absent the guard, so empty child / label history proves
        # the guard short-circuited before the disposition. The operator applied
        # `paused` only after the spawn started, so it appears solely on the
        # freshly fetched view -- a guard reading the stale `issue.labels` would
        # see no hold and split.
        gh = FakeGitHubClient()
        issue = make_issue(310, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            310, user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls
        manifest = _manifest(
            '{"decision": "split", "rationale": "two pieces", "children": ['
            '{"title": "A", "body": "a", "depends_on": []},'
            '{"title": "B", "body": "b", "depends_on": []}]}'
        )

        get_issue_mock = MagicMock(return_value=_paused_view(310, "decomposing"))
        with patch.object(gh, "get_issue", get_issue_mock):
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="dec-sess", last_message=manifest),
            )

        get_issue_mock.assert_called_with(310)
        self.assertEqual(gh.created_child_issues, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        # Durable state untouched: the post-spawn session id is discarded and no
        # pinned-state advancement is written.
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertNotIn("decomposer_session_id", gh.pinned_data(310))


class ReviewerLivePauseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_during_run_blocks_pr_feedback_and_dev_resume(self) -> None:
        # A CHANGES_REQUESTED verdict would post PR feedback, relabel to
        # `fixing`, and resume the dev (a SECOND agent run) absent the guard.
        # The guard stops right after the reviewer returns, so `run_agent` fires
        # exactly once and no relabel / PR comment / pinned-state write lands.
        gh = FakeGitHubClient()
        issue = make_issue(300, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            300,
            pr_number=11,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-300",
            codex_session_id="dev-sess",
            review_round=0,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(300, "validating"))
        with patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
                ),
            )

        mocks["run_agent"].assert_called_once()
        get_issue_mock.assert_called_with(300)
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(300)
        self.assertNotIn("last_review_session_id", state)
        self.assertNotIn("last_review_at", state)


class QuestionLivePauseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_paused_during_fresh_run_blocks_answer_park(self) -> None:
        # The fresh question spawn would post the answer + HITL park and persist
        # the session absent the guard. On a hit the handler posts nothing,
        # writes nothing, and reaps the read-only worktree as on any clean exit
        # (no prior unsafe park, so `keep_worktree` stayed False).
        gh = FakeGitHubClient()
        issue = make_issue(320, label="question", body="Where does X live?")
        gh.add_issue(issue)
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(320, "question"))
        with patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-sess", last_message="X lives in src/x.py:42.",
                ),
            )

        get_issue_mock.assert_called_with(320)
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(320)
        self.assertNotIn("question_session_id", state)
        self.assertFalse(state.get("awaiting_human"))
        mocks["_cleanup_question_worktree"].assert_called_once()

    def test_paused_during_resume_freezes_watermark_advance(self) -> None:
        # Awaiting-human resume: `_resume_question_on_human_reply` advances
        # `last_action_comment_id` past the human's new comment and clears
        # `awaiting_human` in-memory BEFORE the spawn. A `paused` applied during
        # the resume must discard those pre-spawn mutations -- the guard returns
        # without writing, so the next tick re-consumes the same reply.
        gh = FakeGitHubClient()
        issue = make_issue(330, label="question", body="Where does X live?")
        issue.comments.append(
            FakeComment(id=950, body="follow-up", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            330,
            awaiting_human=True,
            last_action_comment_id=900,
            park_reason="question_answer",
            question_agent="claude",
            question_session_id="q-sess-old",
        )
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(330, "question"))
        with patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="q-sess-old", last_message="answer"),
            )

        mocks["run_agent"].assert_called_once()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(330)
        self.assertEqual(state.get("last_action_comment_id"), 900)
        self.assertTrue(state.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
