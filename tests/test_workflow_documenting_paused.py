# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the documenting stage: an operator who applies
`paused` (or `backlog`) WHILE a docs pass is in flight freezes the issue before
the pass's result is published. The guard re-fetches the issue after the resume
returns (`gh.get_issue`) rather than trusting the handler's label snapshot, and
on a hit the handler returns without pushing, posting the docs notice,
advancing to `in_review`, ratcheting watermarks, or writing pinned state -- so
once the label is removed a later tick republishes the committed docs work
through the recovered-worktree path. Covers both docs resumes: the initial pass
and the awaiting-human follow-up."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def _branch(number: int) -> str:
    return f"orchestrator/geserdugarov__agent-orchestrator/issue-{number}"


def _paused_view(number: int) -> object:
    """A `documenting` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run. The handler's own
    `issue` snapshot deliberately does NOT carry it, so a guard that consulted
    the stale snapshot would publish the docs pass."""
    view = make_issue(number, label="documenting")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class DocumentingLivePauseInitialPassTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_pause_during_initial_docs_pass_blocks_push_and_advance(
        self,
    ) -> None:
        # Fresh docs pass on an approved PR. `paused` applied during the pass
        # must stop BEFORE the push / docs notice / advance-to-in_review, so
        # any docs commit stays on the branch and no pinned state advances.
        gh = FakeGitHubClient()
        issue = make_issue(90, label="documenting", body="body")
        gh.add_issue(issue)
        pr = FakePR(number=900, head_branch=_branch(90))
        gh.add_pr(pr)
        seed_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            90,
            user_content_hash=seed_hash,
            dev_agent="codex",
            dev_session_id="dev-sess",
            pr_number=900,
            branch=_branch(90),
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", return_value=_paused_view(90)):
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docs: updated README",
                ),
                head_shas=["before-sha", "after-sha"],
                branch_ahead_behind=(0, 0),
            )

        # No publish, no advance, no docs notice.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Durable state untouched: the pre-spawn `docs_checked_sha` write is
        # discarded and no `docs_verdict` lands.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(90)
        self.assertNotIn("docs_verdict", state)


class DocumentingLivePauseAwaitingHumanTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_pause_during_followup_docs_pass_keeps_park_intact(self) -> None:
        # Awaiting-human docs resume after a park: a human replied and the full
        # docs prompt is rerun. `paused` applied mid-resume must stop before the
        # push / advance / watermark write, leaving the park and the consumed-
        # comment watermark exactly as the prior tick left them.
        gh = FakeGitHubClient()
        issue = make_issue(91, label="documenting", body="body")
        human = FakeComment(id=6000, body="please add a docs note", user=FakeUser("alice"))
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=910, head_branch=_branch(91))
        gh.add_pr(pr)
        # Seed the hash to INCLUDE the new comment so the drift check returns
        # None and the awaiting-human resume (not the drift unwind) handles it.
        seed_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            91,
            user_content_hash=seed_hash,
            dev_agent="codex",
            dev_session_id="dev-sess",
            pr_number=910,
            branch=_branch(91),
            awaiting_human=True,
            park_reason="agent_question",
            last_action_comment_id=5000,
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", return_value=_paused_view(91)):
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docs: added note",
                ),
                head_shas=["before-sha", "after-sha"],
                branch_ahead_behind=(0, 0),
            )

        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Durable state untouched: park stays put, consumed-comment watermark
        # NOT advanced, so the next tick re-consumes the reply.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(91)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("last_action_comment_id"), 5000)


if __name__ == "__main__":
    unittest.main()
